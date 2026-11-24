-- Which audit rows have been sent to an outside trace store (Langfuse). A row here means
-- "sent", so a span goes out once even though exports can run at any time and from any
-- process. The audit tables themselves stay append-only.
--
-- Admin job only: the API role has no access.
create table trace_exports (
  sink text not null,
  source text not null check (source in ('event', 'model_attempt', 'action_attempt')),
  source_id bigint not null,
  exported_at timestamptz not null default now(),
  primary key (sink, source, source_id)
);

alter table trace_exports enable row level security;
grant select, insert, delete on trace_exports to rd_service;
