-- Rename metadata → fields and add per-type structured field support
-- Run this in Supabase: SQL Editor → New query → paste and run

-- 1. Rename the column
alter table zettels rename column metadata to fields;

-- 2. Attachments table
create table if not exists zettel_attachments (
    id           uuid primary key default gen_random_uuid(),
    zettel_id    uuid references zettels(id) on delete cascade,
    filename     text not null,
    storage_path text not null,
    mime_type    text,
    size_bytes   int,
    uploaded_by  text,
    created_at   timestamptz default now()
);

create index if not exists attachments_zettel_idx on zettel_attachments(zettel_id);

-- Enable RLS (service_role bypasses it)
alter table zettel_attachments enable row level security;

-- 3. Supabase Storage bucket for attachments
insert into storage.buckets (id, name, public)
values ('zettel-attachments', 'zettel-attachments', false)
on conflict do nothing;
