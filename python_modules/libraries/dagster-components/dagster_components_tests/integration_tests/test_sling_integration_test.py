import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, Iterator, Mapping, Union

import pytest
import yaml
from dagster import AssetKey
from dagster._core.definitions.events import AssetMaterialization
from dagster._core.definitions.result import MaterializeResult
from dagster._core.execution.context.asset_execution_context import AssetExecutionContext
from dagster._utils.env import environ
from dagster_components.core.component_decl_builder import DefsFileModel
from dagster_components.core.component_defs_builder import (
    YamlComponentDecl,
    build_components_from_component_folder,
)
from dagster_components.impls.sling_replication import SlingReplicationComponent, component
from dagster_embedded_elt.sling import SlingResource

from dagster_components_tests.utils import assert_assets, get_asset_keys, script_load_context

STUB_LOCATION_PATH = Path(__file__).parent.parent / "stub_code_locations" / "sling_location"
COMPONENT_RELPATH = "components/ingest"


def _update_yaml(path: Path, fn) -> None:
    # applies some arbitrary fn to an existing yaml dictionary
    with open(path, "r") as f:
        data = yaml.safe_load(f)
    with open(path, "w") as f:
        yaml.dump(fn(data), f)


@contextmanager
@pytest.fixture(scope="module")
def sling_path() -> Generator[Path, None, None]:
    """Sets up a temporary directory with a replication.yaml and defs.yml file that reference
    the proper temp path.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        with environ({"HOME": temp_dir}):
            shutil.copytree(STUB_LOCATION_PATH, temp_dir, dirs_exist_ok=True)

            # update the replication yaml to reference a CSV file in the tempdir
            replication_path = Path(temp_dir) / COMPONENT_RELPATH / "replication.yaml"

            def _update_replication(data: Dict[str, Any]) -> Mapping[str, Any]:
                placeholder_data = data["streams"].pop("<PLACEHOLDER>")
                data["streams"][f"file://{temp_dir}/input.csv"] = placeholder_data
                return data

            _update_yaml(replication_path, _update_replication)

            # update the defs yaml to add a duckdb instance
            defs_path = Path(temp_dir) / COMPONENT_RELPATH / "defs.yml"

            def _update_defs(data: Dict[str, Any]) -> Mapping[str, Any]:
                data["component_params"]["sling"]["connections"][0]["instance"] = (
                    f"{temp_dir}/duckdb"
                )
                return data

            _update_yaml(defs_path, _update_defs)

            yield Path(temp_dir)


def test_python_params(sling_path: Path) -> None:
    context = script_load_context()
    component = SlingReplicationComponent.from_decl_node(
        context=context,
        decl_node=YamlComponentDecl(
            path=sling_path / COMPONENT_RELPATH,
            defs_file_model=DefsFileModel(
                component_type="sling_replication",
                component_params={"sling": {}},
            ),
        ),
    )
    assert component.op_spec is None
    assert get_asset_keys(component) == {
        AssetKey("input_csv"),
        AssetKey("input_duckdb"),
    }

    defs = component.build_defs(context)
    # inherited from directory name
    assert defs.get_assets_def("input_duckdb").op.name == "ingest"


def test_python_params_op_name(sling_path: Path) -> None:
    context = script_load_context()
    component = SlingReplicationComponent.from_decl_node(
        context=context,
        decl_node=YamlComponentDecl(
            path=sling_path / COMPONENT_RELPATH,
            defs_file_model=DefsFileModel(
                component_type="sling_replication",
                component_params={"sling": {}, "op": {"name": "my_op"}},
            ),
        ),
    )
    assert component.op_spec
    assert component.op_spec.name == "my_op"
    defs = component.build_defs(context)
    assert defs.get_asset_graph().get_all_asset_keys() == {
        AssetKey("input_csv"),
        AssetKey("input_duckdb"),
    }

    assert defs.get_assets_def("input_duckdb").op.name == "my_op"


def test_python_params_op_tags(sling_path: Path) -> None:
    context = script_load_context()
    component = SlingReplicationComponent.from_decl_node(
        context=context,
        decl_node=YamlComponentDecl(
            path=sling_path / COMPONENT_RELPATH,
            defs_file_model=DefsFileModel(
                component_type="sling_replication",
                component_params={"sling": {}, "op": {"tags": {"tag1": "value1"}}},
            ),
        ),
    )
    assert component.op_spec
    assert component.op_spec.tags == {"tag1": "value1"}
    defs = component.build_defs(context)
    assert defs.get_assets_def("input_duckdb").op.tags == {"tag1": "value1"}


def test_load_from_path(sling_path: Path) -> None:
    components = build_components_from_component_folder(
        script_load_context(), sling_path / "components"
    )
    assert len(components) == 1
    assert get_asset_keys(components[0]) == {
        AssetKey("input_csv"),
        AssetKey("input_duckdb"),
    }

    assert_assets(components[0], 2)


def test_sling_subclass() -> None:
    @component(name="debug_sling_replication")
    class DebugSlingReplicationComponent(SlingReplicationComponent):
        def execute(
            self, context: AssetExecutionContext, sling: SlingResource
        ) -> Iterator[Union[AssetMaterialization, MaterializeResult]]:
            return sling.replicate(context=context, debug=True)

    component_inst = DebugSlingReplicationComponent.from_decl_node(
        context=script_load_context(),
        decl_node=YamlComponentDecl(
            path=STUB_LOCATION_PATH / COMPONENT_RELPATH,
            defs_file_model=DefsFileModel(
                component_type="debug_sling_replication",
                component_params={"sling": {}},
            ),
        ),
    )
    assert get_asset_keys(component_inst) == {
        AssetKey("input_csv"),
        AssetKey("input_duckdb"),
    }