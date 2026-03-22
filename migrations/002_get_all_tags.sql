-- Returns all unique tags currently in use, sorted alphabetically
create or replace function get_all_tags()
returns text[]
language sql
stable
as $$
    select coalesce(array_agg(distinct tag order by tag), '{}')
    from zettels, unnest(tags) as tag
$$;
