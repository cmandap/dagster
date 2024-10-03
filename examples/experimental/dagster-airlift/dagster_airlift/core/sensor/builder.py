from datetime import timedelta
from typing import Iterable, Iterator, List, Optional, Sequence, Set

from dagster import (
    AssetCheckKey,
    AssetKey,
    AssetMaterialization,
    DefaultSensorStatus,
    RunRequest,
    SensorEvaluationContext,
    SensorResult,
    _check as check,
    sensor,
)
from dagster._core.definitions.asset_selection import AssetSelection
from dagster._core.definitions.definitions_class import Definitions
from dagster._core.definitions.repository_definition.repository_definition import (
    RepositoryDefinition,
)
from dagster._core.storage.dagster_run import RunsFilter
from dagster._grpc.client import DEFAULT_SENSOR_GRPC_TIMEOUT
from dagster._record import record
from dagster._serdes import deserialize_value, serialize_value
from dagster._serdes.serdes import whitelist_for_serdes
from dagster._time import datetime_from_timestamp, get_current_datetime

from dagster_airlift.constants import (
    AUTOMAPPED_TASK_METADATA_KEY,
    DAG_RUN_ID_TAG_KEY,
    TASK_ID_TAG_KEY,
)
from dagster_airlift.core.airflow_defs_data import AirflowDefinitionsData
from dagster_airlift.core.airflow_instance import AirflowInstance, DagRun, TaskInstance
from dagster_airlift.core.sensor.event_translation import (
    AirflowEventTranslationFn,
    get_timestamp_from_materialization,
    materializations_for_dag_run,
    synthetic_mats_for_mapped_asset_keys,
    synthetic_mats_for_task_instance,
)

MAIN_LOOP_TIMEOUT_SECONDS = DEFAULT_SENSOR_GRPC_TIMEOUT - 20
DEFAULT_AIRFLOW_SENSOR_INTERVAL_SECONDS = 1
START_LOOKBACK_SECONDS = 60  # Lookback one minute in time for the initial setting of the cursor.


@whitelist_for_serdes
@record
class AirflowPollingSensorCursor:
    """A cursor that stores the last effective timestamp and the last polled dag id."""

    end_date_gte: Optional[float] = None
    end_date_lte: Optional[float] = None
    dag_query_offset: Optional[int] = None


def check_keys_for_asset_keys(
    repository_def: RepositoryDefinition, asset_keys: Set[AssetKey]
) -> Iterable[AssetCheckKey]:
    for assets_def in repository_def.asset_graph.assets_defs:
        for check_spec in assets_def.check_specs:
            if check_spec.asset_key in asset_keys:
                yield check_spec.key


def build_airflow_polling_sensor_defs(
    airflow_data: AirflowDefinitionsData,
    event_translation_fn: Optional[AirflowEventTranslationFn],
    minimum_interval_seconds: int = DEFAULT_AIRFLOW_SENSOR_INTERVAL_SECONDS,
) -> Definitions:
    @sensor(
        name=f"{airflow_data.airflow_instance.name}__airflow_dag_status_sensor",
        minimum_interval_seconds=minimum_interval_seconds,
        default_status=DefaultSensorStatus.RUNNING,
        # This sensor will only ever execute asset checks and not asset materializations.
        asset_selection=AssetSelection.all_asset_checks(),
    )
    def airflow_dag_sensor(context: SensorEvaluationContext) -> SensorResult:
        """Sensor to report materialization events for each asset as new runs come in."""
        context.log.info(
            f"************Running sensor for {airflow_data.airflow_instance.name}***********"
        )
        try:
            cursor = (
                deserialize_value(context.cursor, AirflowPollingSensorCursor)
                if context.cursor
                else AirflowPollingSensorCursor()
            )
        except Exception as e:
            context.log.info(f"Failed to interpret cursor. Starting from scratch. Error: {e}")
            cursor = AirflowPollingSensorCursor()
        current_date = get_current_datetime()
        current_dag_offset = cursor.dag_query_offset or 0
        end_date_gte = (
            cursor.end_date_gte
            or (current_date - timedelta(seconds=START_LOOKBACK_SECONDS)).timestamp()
        )
        end_date_lte = cursor.end_date_lte or current_date.timestamp()
        sensor_iter = materializations_and_requests_from_batch_iter(
            context=context,
            end_date_gte=end_date_gte,
            end_date_lte=end_date_lte,
            offset=current_dag_offset,
            airflow_data=airflow_data,
            event_translation_fn=event_translation_fn,
        )
        all_asset_events: List[AssetMaterialization] = []
        all_check_keys: Set[AssetCheckKey] = set()
        latest_offset = current_dag_offset
        repository_def = check.not_none(context.repository_def)
        while get_current_datetime() - current_date < timedelta(seconds=MAIN_LOOP_TIMEOUT_SECONDS):
            batch_result = next(sensor_iter, None)
            if batch_result is None:
                break
            all_asset_events.extend(batch_result.asset_events)

            all_check_keys.update(
                check_keys_for_asset_keys(repository_def, batch_result.all_asset_keys_materialized)
            )
            latest_offset = batch_result.idx

        if batch_result is not None:
            new_cursor = AirflowPollingSensorCursor(
                end_date_gte=end_date_gte,
                end_date_lte=end_date_lte,
                dag_query_offset=latest_offset + 1,
            )
        else:
            # We have completed iteration for this range
            new_cursor = AirflowPollingSensorCursor(
                end_date_gte=end_date_lte,
                end_date_lte=None,
                dag_query_offset=0,
            )
        context.update_cursor(serialize_value(new_cursor))

        context.log.info(
            f"************Exitting sensor for {airflow_data.airflow_instance.name}***********"
        )
        return SensorResult(
            asset_events=sorted_asset_events(all_asset_events, repository_def),
            run_requests=[RunRequest(asset_check_keys=list(all_check_keys))]
            if all_check_keys
            else None,
        )

    return Definitions(sensors=[airflow_dag_sensor])


def sorted_asset_events(
    all_materializations: Sequence[AssetMaterialization],
    repository_def: RepositoryDefinition,
) -> List[AssetMaterialization]:
    """Sort materializations by end date and toposort order."""
    topo_aks = repository_def.asset_graph.toposorted_asset_keys
    materializations_and_timestamps = [
        (get_timestamp_from_materialization(mat), mat) for mat in all_materializations
    ]
    return [
        sorted_mat[1]
        for sorted_mat in sorted(
            materializations_and_timestamps, key=lambda x: (x[0], topo_aks.index(x[1].asset_key))
        )
    ]


@record
class BatchResult:
    idx: int
    asset_events: Sequence[AssetMaterialization]
    all_asset_keys_materialized: Set[AssetKey]


def materializations_and_requests_from_batch_iter(
    context: SensorEvaluationContext,
    end_date_gte: float,
    end_date_lte: float,
    offset: int,
    airflow_data: AirflowDefinitionsData,
    event_translation_fn: Optional[AirflowEventTranslationFn],
) -> Iterator[Optional[BatchResult]]:
    runs = airflow_data.airflow_instance.get_dag_runs_batch(
        dag_ids=list(airflow_data.all_dag_ids),
        end_date_gte=datetime_from_timestamp(end_date_gte),
        end_date_lte=datetime_from_timestamp(end_date_lte),
        offset=offset,
    )
    context.log.info(f"Found {len(runs)} dag runs for {airflow_data.airflow_instance.name}")
    context.log.info(f"All runs {runs}")
    for i, dag_run in enumerate(runs):
        # TODO: add pluggability here (ignoring `event_translation_fn` for now)

        dag_mats = materializations_for_dag_run(dag_run, airflow_data)
        synthetic_mats = build_synthetic_asset_materializations(
            context, airflow_data.airflow_instance, dag_run, airflow_data
        )
        mats = list(dag_mats) + synthetic_mats
        context.log.info(f"Found {len(mats)} materializations for {dag_run.run_id}")

        all_asset_keys_materialized = {mat.asset_key for mat in mats}
        yield (
            BatchResult(
                idx=i + offset,
                asset_events=mats,
                all_asset_keys_materialized=all_asset_keys_materialized,
            )
            if mats
            else None
        )


def build_synthetic_asset_materializations(
    context: SensorEvaluationContext,
    airflow_instance: AirflowInstance,
    dag_run: DagRun,
    airflow_data: AirflowDefinitionsData,
) -> List[AssetMaterialization]:
    """In this function we need to return the asset materializations we want to synthesize
    on behalf of the user.

    This happens when the user has modeled an external asset that has a corresponding
    task in airflow which is not proxied to Dagster. We want to detect the case
    where there is a successful airflow task instance that is mapped to a
    dagster asset but is _not_ proxied, and then synthensize a materialization
    for observability.

    We do this by querying for successful task instances in Airflow. And then
    for each successful task we see it there exists a Dagster Run tagged with
    the run id. If there is not Dagster run, we know the task was not proxied.

    Task instances are mutable in Airflow, so we are not guaranteed to register
    every task instance. If, for example, the sensor is paused, and then there are
    multiple task clearings, we will only register the last materialization.

    This also currently does not support dynamic tasks in Airflow, in which case
    the use should instead map at the dag-level granularity.
    """
    task_instances = airflow_instance.get_task_instance_batch(
        run_id=dag_run.run_id,
        dag_id=dag_run.dag_id,
        task_ids=[task_id for task_id in airflow_data.task_ids_in_dag(dag_run.dag_id)],
        states=["success"],
    )

    context.log.info(f"Found {len(task_instances)} task instances for {dag_run.run_id}")
    context.log.info(f"All task instances {task_instances}")

    check.invariant(
        len({ti.task_id for ti in task_instances}) == len(task_instances),
        "Assuming one task instance per task_id for now. Dynamic Airflow tasks not supported.",
    )

    # https://linear.app/dagster-labs/issue/FOU-444/make-sensor-work-with-an-airflow-dag-run-that-has-more-than-1000
    dagster_runs = context.instance.get_runs(
        filters=RunsFilter(tags={DAG_RUN_ID_TAG_KEY: dag_run.run_id}),
        limit=1000,
    )

    context.log.info(
        f"Airlift Sensor: Found dagster run ids: {[run.run_id for run in dagster_runs]}"
        f" for airflow run id {dag_run.run_id} and dag id {dag_run.dag_id}"
    )

    dagster_runs_by_task_id = {run.tags[TASK_ID_TAG_KEY]: run for run in dagster_runs}
    task_instances_by_task_id = {ti.task_id: ti for ti in task_instances}

    synthetic_mats = []

    for task_id, task_instance in task_instances_by_task_id.items():
        # If there is no dagster_run for this task, it was not proxied.
        # Therefore synthensize a materialization based on the task information.
        if task_id not in dagster_runs_by_task_id:
            context.log.info(
                f"Synthesizing materialization for tasks {task_id} in dag {dag_run.dag_id} because no dagster run found."
            )
            synthetic_mats.extend(
                synthetic_mats_for_task_instance(airflow_data, dag_run, task_instance)
            )
        else:
            # We *always* emit for the automapped tasks, even if they are proxied
            asset_keys_to_emit = automapped_tasks_asset_keys(dag_run, airflow_data, task_instance)

            synthetic_mats.extend(
                synthetic_mats_for_mapped_asset_keys(
                    dag_run=dag_run, task_instance=task_instance, asset_keys=asset_keys_to_emit
                )
            )

            context.log.info(
                f"Dagster run found for task {task_id} in dag {dag_run.dag_id}. Run {dagster_runs_by_task_id[task_id].run_id}"
            )

    return synthetic_mats


def automapped_tasks_asset_keys(
    dag_run: DagRun, airflow_data: AirflowDefinitionsData, task_instance: TaskInstance
) -> Set[AssetKey]:
    asset_keys_to_emit = set()
    asset_keys = airflow_data.asset_keys_in_task(dag_run.dag_id, task_instance.task_id)
    for asset_key in asset_keys:
        spec = airflow_data.resolved_airflow_defs.get_assets_def(asset_key).get_asset_spec(
            asset_key
        )
        if spec.metadata.get(AUTOMAPPED_TASK_METADATA_KEY):
            asset_keys_to_emit.add(asset_key)
    return asset_keys_to_emit
