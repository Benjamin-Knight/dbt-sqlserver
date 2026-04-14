{% macro sqlserver__table_dml_refresh(target_relation, sql) %}
  {#
    DML-only table refresh for use under RCSI.

    Instead of rename-swap (which uses DDL and creates a window where the
    table name doesnt resolve), this macro:
    1. Builds new data into a scratch table via SELECT INTO (minimally logged)
    2. Compares schemas — if columns changed, falls back to rename-swap
    3. Swaps data via DELETE + INSERT inside an explicit transaction
       (RCSI ensures concurrent readers see old data until COMMIT)
    4. Cleans up the scratch table

    The scratch table is a regular table with a __dbt_refresh suffix,
    not a global temp table. This avoids cross-session visibility issues
    and ensures cleanup on failure (DROP IF EXISTS at the start of each run).
  #}

  {%- set refresh_relation = target_relation.incorporate(
      path={"identifier": target_relation.identifier ~ '__dbt_refresh'}
  ) -%}

  {# Clean up any leftover scratch table from a prior failed run #}
  {% call statement('dml_refresh_cleanup_pre') -%}
    DROP TABLE IF EXISTS {{ refresh_relation }};
  {%- endcall %}

  {# Build new data into scratch table (heap — minimally logged under SIMPLE recovery) #}
  {# Named 'main' because dbt requires a statement('main') call in every materialization #}
  {% call statement('main') -%}
    SELECT * INTO {{ refresh_relation }} FROM ({{ sql }}) AS __dbt_sbq;
  {%- endcall %}

  {# Compare schemas: if columns differ, fall back to rename-swap #}
  {%- set schema_changes = check_for_schema_changes(refresh_relation, target_relation) -%}
  {%- set schema_match = not schema_changes['schema_changed'] -%}

  {% if schema_match %}
    {# Atomic DML swap — RCSI protects concurrent readers #}
    {# dbt-sqlserver uses autocommit=True and add_begin_query/add_commit_query #}
    {# are no-ops, so this creates a simple (non-nested) transaction. #}
    {% call statement('dml_refresh_swap') -%}
      BEGIN TRANSACTION;
      DELETE FROM {{ target_relation }};
      INSERT INTO {{ target_relation }}
        SELECT * FROM {{ refresh_relation }};
      COMMIT TRANSACTION;
    {%- endcall %}

    {# Cleanup scratch table #}
    {% call statement('dml_refresh_cleanup_post') -%}
      DROP TABLE IF EXISTS {{ refresh_relation }};
    {%- endcall %}

  {% else %}
    {# Schema changed — fall back to rename-swap for this run #}
    {{ log("Schema change detected for " ~ target_relation ~ " — falling back to rename-swap", info=true) }}

    {%- set backup_relation_type = target_relation.type -%}
    {%- set backup_relation = make_backup_relation(target_relation, backup_relation_type) -%}
    {{ drop_relation_if_exists(backup_relation) }}

    {# Rename scratch table into position #}
    {% set existing_relation = load_cached_relation(target_relation) %}
    {% if existing_relation is not none %}
      {{ adapter.rename_relation(existing_relation, backup_relation) }}
    {% endif %}

    {{ adapter.rename_relation(refresh_relation, target_relation) }}

    {% do create_indexes(target_relation) %}

    {{ drop_relation_if_exists(backup_relation) }}

    {# scratch table is now the target, nothing to drop #}
  {% endif %}

{% endmacro %}
