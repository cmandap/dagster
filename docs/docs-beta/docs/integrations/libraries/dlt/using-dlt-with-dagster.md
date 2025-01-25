---
title: "Using dlt with Dagster"
description: Ingest data with ease using Dagster and dlt
---

:::

This feature is considered **experimental**

:::

The [data load tool (dlt)](https://dlthub.com/) open-source library defines a standardized approach for creating data pipelines that load often messy data sources into well-structured data sets. It offers many advanced features, such as:

- Handling connection secrets
- Converting data into the structure required for a destination
- Incremental updates and merges

dlt also provides a large collection of [pre-built, verified sources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/) and [destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/), allowing you to write less code (if any!) by leveraging the work of the dlt community.

In this guide, we'll explain how the dlt integration works, how to set up a Dagster project for dlt, and how to use a pre-defined dlt source.

## How it works

The Dagster dlt integration uses [multi-assets](/guides/build/assets/defining-assets#multi-asset), a single definition that results in multiple assets. These assets are derived from the `DltSource`.

The following is an example of a dlt source definition where a source is made up of two resources:

```python
@dlt.source
def example(api_key: str = dlt.secrets.value):
    @dlt.resource(primary_key="id", write_disposition="merge")
    def courses():
        response = requests.get(url=BASE_URL + "courses")
        response.raise_for_status()
        yield response.json().get("items")

    @dlt.resource(primary_key="id", write_disposition="merge")
    def users():
        for page in _paginate(BASE_URL + "users"):
            yield page

    return courses, users
```

Each resource queries an API endpoint and yields the data that we wish to load into our data warehouse. The two resources defined on the source will map to Dagster assets.

Next, we defined a dlt pipeline that specifies how we want the data to be loaded:

```python
pipeline = dlt.pipeline(
    pipeline_name="example_pipeline",
    destination="snowflake",
    dataset_name="example_data",
    progress="log",
)
```

A dlt source and pipeline are the two components required to load data using dlt. These will be the parameters of our multi-asset, which will integrate dlt and Dagster.

## Prerequisites

To follow the steps in this guide, you'll need:

- **To read the [dlt introduction](https://dlthub.com/docs/intro)**, if you've never worked with dlt before.
- **[To install](/getting-started/installation) the following libraries**:

  ```bash
  pip install dagster dagster-dlt
  ```

  Installing `dagster-dlt` will also install the `dlt` package.

## Step 1: Configure your Dagster project to support dlt

The first step is to define a location for the `dlt` code used for ingesting data. We recommend creating a `dlt_sources` directory at the root of your Dagster project, but this code can reside anywhere within your Python project.

Run the following to create the `dlt_sources` directory:

```bash
cd $DAGSTER_HOME && mkdir dlt_sources
```

## Step 2: Initialize dlt ingestion code

In the `dlt_sources` directory, you can write ingestion code following the [dlt tutorial](https://dlthub.com/docs/tutorial/load-data-from-an-api) or you can use a verified source.

In this example, we'll use the [GitHub source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/github) provided by dlt.

1. Run the following to create a location for the dlt source code and initialize the GitHub source:

   ```bash
   cd dlt_sources

   dlt init github snowflake
   ```

   At which point you'll see the following in the command line:

   ```bash
   Looking up the init scripts in https://github.com/dlt-hub/verified-sources.git...
   Cloning and configuring a verified source github (Source that load github issues, pull requests and reactions for a specific repository via customizable graphql query. Loads events incrementally.)
   ```

2. When prompted to proceed, enter `y`. You should see the following confirming that the GitHub source was added to the project:

   ```bash
   Verified source github was added to your project!
   * See the usage examples and code snippets to copy from github_pipeline.py
   * Add credentials for snowflake and other secrets in ./.dlt/secrets.toml
   * requirements.txt was created. Install it with:
   pip3 install -r requirements.txt
   * Read https://dlthub.com/docs/walkthroughs/create-a-pipeline for more information
   ```

This downloaded the code required to collect data from the GitHub API. It also created a `requirements.txt` and a `.dlt/` configuration directory. These files can be removed, as we will configure our pipelines through Dagster, however, you may still find it informative to reference.

```bash
$ tree -a
.
├── .dlt               # can be removed
│   ├── .sources
│   ├── config.toml
│   └── secrets.toml
├── .gitignore
├── github
│   ├── README.md
│   ├── __init__.py
│   ├── helpers.py
│   ├── queries.py
│   └── settings.py
├── github_pipeline.py
└── requirements.txt   # can be removed
```

## Step 3: Define dlt environment variables

This integration manages connections and secrets using environment variables as `dlt`. The `dlt` library can infer required environment variables used by its sources and resources. Refer to [dlt's Secrets and Configs](https://dlthub.com/docs/general-usage/credentials/configuration) documentation for more information.

In the example we've been using:

- The `github_reactions` source requires a GitHub access token
- The Snowflake destination requires database connection details

This results in the following required environment variables:

```bash
SOURCES__GITHUB__ACCESS_TOKEN=""
DESTINATION__SNOWFLAKE__CREDENTIALS__DATABASE=""
DESTINATION__SNOWFLAKE__CREDENTIALS__PASSWORD=""
DESTINATION__SNOWFLAKE__CREDENTIALS__USERNAME=""
DESTINATION__SNOWFLAKE__CREDENTIALS__HOST=""
DESTINATION__SNOWFLAKE__CREDENTIALS__WAREHOUSE=""
DESTINATION__SNOWFLAKE__CREDENTIALS__ROLE=""
```

Ensure that these variables are defined in your environment, either in your `.env` file when running locally or in the [Dagster deployment's environment variables](/guides/deploy/using-environment-variables-and-secrets).

## Step 4: Define a DagsterDltResource

Next, we'll define a <PyObject section="libraries" module="dagster_dlt" object="DagsterDltResource" />, which provides a wrapper of a dlt pipeline runner. Use the following to define the resource, which can be shared across all dlt pipelines:

```python
from dagster_dlt import DagsterDltResource

dlt_resource = DagsterDltResource()
```

We'll add the resource to our <PyObject section="definitions" module="dagster" object="Definitions" /> in a later step.

## Step 5: Create a dlt_assets definition for GitHub

The <PyObject section="libraries" object="dlt_assets" module="dagster_dlt" decorator /> decorator takes a `dlt_source` and `dlt_pipeline` parameter. In this example, we used the `github_reactions` source and created a `dlt_pipeline` to ingest data from Github to Snowflake.

In the same file containing your Dagster assets, you can create an instance of your <PyObject section="libraries" object="dlt_assets" module="dagster_dlt" decorator /> by doing something like the following:

:::

If you are using the [sql_database](https://dlthub.com/docs/api_reference/sources/sql_database/__init__#sql_database) source, consider setting `defer_table_reflect=True` to reduce database reads. By default, the Dagster daemon will refresh definitions roughly every minute, which will query the database for resource definitions.

:::

```python
from dagster import AssetExecutionContext, Definitions
from dagster_dlt import DagsterDltResource, dlt_assets
from dlt import pipeline
from dlt_sources.github import github_reactions


@dlt_assets(
    dlt_source=github_reactions(
        "dagster-io", "dagster", max_items=250
    ),
    dlt_pipeline=pipeline(
        pipeline_name="github_issues",
        dataset_name="github",
        destination="snowflake",
        progress="log",
    ),
    name="github",
    group_name="github",
)
def dagster_github_assets(context: AssetExecutionContext, dlt: DagsterDltResource):
    yield from dlt.run(context=context)
```

## Step 6: Create the Definitions object

The last step is to include the assets and resource in a <PyObject section="definitions" module="dagster" object="Definitions" /> object. This enables Dagster tools to load everything we've defined:

```python
defs = Definitions(
    assets=[
        dagster_github_assets,
    ],
    resources={
        "dlt": dlt_resource,
    },
)
```

And that's it! You should now have two assets that load data to corresponding Snowflake tables: one for issues and the other for pull requests.

## Advanced usage

### Overriding the translator to customize dlt assets

The <PyObject section="libraries" module="dagster_dlt" object="DagsterDltTranslator" /> object can be used to customize how dlt properties map to Dagster concepts.

For example, to change how the name of the asset is derived, or if you would like to change the key of the upstream source asset, you can override the <PyObject section="libraries" module="dagster_dlt" object="DagsterDltTranslator" method="get_asset_spec" /> method.

{/* TODO convert to <CodeExample> */}
```python file=/integrations/dlt/dlt_dagster_translator.py
import dlt
from dagster_dlt import DagsterDltResource, DagsterDltTranslator, dlt_assets
from dagster_dlt.translator import DltResourceTranslatorData

from dagster import AssetExecutionContext, AssetKey, AssetSpec


@dlt.source
def example_dlt_source():
    def example_resource(): ...

    return example_resource


class CustomDagsterDltTranslator(DagsterDltTranslator):
    def get_asset_spec(self, data: DltResourceTranslatorData) -> AssetSpec:
        """Overrides asset spec to:
        - Override asset key to be the dlt resource name,
        - Override upstream asset key to be a single source asset.
        """
        default_spec = super().get_asset_spec(data)
        return default_spec.replace_attributes(
            key=AssetKey(f"{data.resource.name}"),
            deps=[AssetKey("common_upstream_dlt_dependency")],
        )


@dlt_assets(
    name="example_dlt_assets",
    dlt_source=example_dlt_source(),
    dlt_pipeline=dlt.pipeline(
        pipeline_name="example_pipeline_name",
        dataset_name="example_dataset_name",
        destination="snowflake",
        progress="log",
    ),
    dagster_dlt_translator=CustomDagsterDltTranslator(),
)
def dlt_example_assets(context: AssetExecutionContext, dlt: DagsterDltResource):
    yield from dlt.run(context=context)
```

In this example, we customized the translator to change how the dlt assets' names are defined. We also hard-coded the asset dependency upstream of our assets to provide a fan-out model from a single dependency to our dlt assets.

### Assigning metadata to upstream external assets

A common question is how to define metadata on the external assets upstream of the dlt assets.

This can be accomplished by defining a <PyObject section="assets" module="dagster" object="AssetSpec" /> with a key that matches the one defined in the <PyObject section="libraries" module="dagster_dlt" object="DagsterDltTranslator" method="get_asset_spec" /> method.

For example, let's say we have defined a set of dlt assets named `thinkific_assets`, we can iterate over those assets and derive a <PyObject section="assets" module="dagster" object="AssetSpec" /> with attributes like `group_name`.

{/* TODO convert to <CodeExample> */}
```python file=/integrations/dlt/dlt_source_assets.py
import dlt
from dagster_dlt import DagsterDltResource, dlt_assets

from dagster import AssetExecutionContext, AssetSpec


@dlt.source
def example_dlt_source():
    def example_resource(): ...

    return example_resource


@dlt_assets(
    dlt_source=example_dlt_source(),
    dlt_pipeline=dlt.pipeline(
        pipeline_name="example_pipeline_name",
        dataset_name="example_dataset_name",
        destination="snowflake",
        progress="log",
    ),
)
def example_dlt_assets(context: AssetExecutionContext, dlt: DagsterDltResource):
    yield from dlt.run(context=context)


thinkific_source_assets = [
    AssetSpec(key, group_name="thinkific") for key in example_dlt_assets.dependency_keys
]
```

### Using partitions in your dlt assets

While still an experimental feature, it is possible to use partitions within your dlt assets. However, it should be noted that this may result in concurrency related issues as state is managed by dlt. For this reason, it is recommended to set concurrency limits for your partitioned dlt assets. See the [Limiting concurrency in data pipelines](/guides/operate/managing-concurrency) guide for more details.

That said, here is an example of using static named partitions from a dlt source.

{/* TODO convert to <CodeExample> */}
```python file=/integrations/dlt/dlt_partitions.py
from typing import Optional

import dlt
from dagster_dlt import DagsterDltResource, dlt_assets

from dagster import AssetExecutionContext, StaticPartitionsDefinition

color_partitions = StaticPartitionsDefinition(["red", "green", "blue"])


@dlt.source
def example_dlt_source(color: Optional[str] = None):
    def load_colors():
        if color:
            # partition-specific processing
            ...
        else:
            # non-partitioned processing
            ...


@dlt_assets(
    dlt_source=example_dlt_source(),
    name="example_dlt_assets",
    dlt_pipeline=dlt.pipeline(
        pipeline_name="example_pipeline_name",
        dataset_name="example_dataset_name",
        destination="snowflake",
    ),
    partitions_def=color_partitions,
)
def compute(context: AssetExecutionContext, dlt: DagsterDltResource):
    color = context.partition_key
    yield from dlt.run(context=context, dlt_source=example_dlt_source(color=color))
```

## What's next?

Want to see real-world examples of dlt in production? Check out how we use it internally at Dagster in the [Dagster Open Platform](https://github.com/dagster-io/dagster-open-platform) project.