{% macro sqlserver__create_table_as(temporary, relation, sql) -%}
    {%- set query_label = get_query_options(parse_options=True) -%}
    {%- set full_refresh_build = config.get('full_refresh_build', 'heap_then_index') -%}
    {%- if full_refresh_build not in ['heap_then_index', 'prebuilt'] -%}
      {{ exceptions.raise_compiler_error(
        "Invalid full_refresh_build '" ~ full_refresh_build ~ "'. "
        "Valid values are: 'heap_then_index' (default), 'prebuilt'."
      ) }}
    {%- endif -%}
    {%- set tmp_relation = relation.incorporate(path={"identifier": relation.identifier ~ '__dbt_tmp_vw'}, type='view') -%}

    {%- do adapter.drop_relation(tmp_relation) -%}
    USE [{{ relation.database }}];
    {{ get_create_view_as_sql(tmp_relation, sql) }}

    {%- set table_name -%}
        {{ relation }}
    {%- endset -%}


    {%- set contract_config = config.get('contract') -%}
    {%- set as_columnstore = config.get('as_columnstore', default=true) -%}
    {%- set contract_enforced = contract_config.enforced and (not temporary) -%}
    {#- prebuilt: create the relation EMPTY with its clustered design already in
        place, then bulk-load with TABLOCK. Avoids the uncompressed-heap stage
        (peak disk = heap + index-build sort) of the default path; on an empty
        clustered target the TABLOCK insert is minimally logged under
        SIMPLE/bulk-logged recovery and rows compress on insert. The index is
        created on this (possibly intermediate) relation but NAMED for `this`
        (the final target) so the name survives the rename swap and the
        post-swap create_indexes/reconcile see the expected name.
        Trade-off (documented, no runtime warning): on Enterprise editions the
        rowstore load phase serializes on the B-tree insert and can be ~2x
        slower wall-clock than heap-then-parallel-index. -#}
    {%- set use_prebuilt = full_refresh_build == 'prebuilt' and not temporary and not contract_enforced -%}

    {#- rowstore prebuilt needs a clustered entry in the indexes config to
        pre-create; without one there is nothing to prebuild, so warn and use
        the default heap path. validate_indexes guarantees at most one
        clustered entry and rejects clustered x as_columnstore conflicts. -#}
    {%- set prebuilt_ns = namespace(clustered_dict=none) -%}
    {%- if use_prebuilt and not as_columnstore -%}
        {%- for raw_index in config.get('indexes', default=[]) -%}
            {%- set parsed = adapter.parse_index(raw_index) -%}
            {%- if parsed and parsed.type == 'clustered' and prebuilt_ns.clustered_dict is none -%}
                {%- set prebuilt_ns.clustered_dict = raw_index -%}
            {%- endif -%}
        {%- endfor -%}
        {%- if prebuilt_ns.clustered_dict is none -%}
            {{ log("full_refresh_build=prebuilt on " ~ this ~ " requires a clustered index in the indexes config; falling back to heap_then_index", info=true) }}
            {%- set use_prebuilt = false -%}
        {%- endif -%}
    {%- endif -%}

    {% if use_prebuilt %}
        {{ log("Building " ~ this ~ " with full_refresh_build=prebuilt", info=true) }}
        EXEC('SELECT TOP 0 * INTO {{ table_name }} FROM {{ tmp_relation }}')

        {% if as_columnstore %}
            {{ sqlserver__create_clustered_columnstore_index(relation, name_relation=this) }}
        {% else %}
            {{ sqlserver__get_create_index_sql(relation, prebuilt_ns.clustered_dict, name_relation=this) }}
        {% endif %}

        {%- set insert_query -%}
            INSERT INTO {{ table_name }} WITH (TABLOCK)
            SELECT * FROM {{ tmp_relation }} {{ query_label }}
        {%- endset %}
        EXEC('{{- escape_single_quotes(insert_query) -}}')

        EXEC('DROP VIEW IF EXISTS {{ tmp_relation.include(database=False) }}')
    {% else %}
        {%- set query -%}
            {% if contract_enforced %}
                CREATE TABLE {{table_name}}
                {{ get_assert_columns_equivalent(sql)  }}
                {{ build_columns_constraints(relation) }}
                {% set listColumns %}
                    {% for column in model['columns'] %}
                        {{ "["~column~"]" }}{{ ", " if not loop.last }}
                    {% endfor %}
                {%endset%}
                INSERT INTO {{relation}} WITH (TABLOCK) ({{listColumns}})
                SELECT {{listColumns}} FROM {{tmp_relation}} {{ query_label }}

            {% else %}
                SELECT * INTO {{ table_name }} FROM {{ tmp_relation }} {{ query_label }}
            {% endif %}
        {%- endset -%}

        EXEC('{{- escape_single_quotes(query) -}}')

        {# For some reason drop_relation is not firing. This solves the issue for now. #}
        EXEC('DROP VIEW IF EXISTS {{ tmp_relation.include(database=False) }}')

        {% if not temporary and as_columnstore -%}
            {#-
            add columnstore index
            this creates with dbt_temp as its coming from a temporary relation before renaming
            could alter relation to drop the dbt_temp portion if needed
            -#}
            {{ sqlserver__create_clustered_columnstore_index(relation) }}
        {% endif %}
    {% endif %}

{% endmacro %}
