-- Add author field to track who submitted each zettel
-- Run this in Supabase: SQL Editor → New query → paste and run

alter table zettels add column if not exists author text;

create index if not exists zettels_author_idx on zettels(author);
