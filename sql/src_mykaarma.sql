-- One-time setup (re-runnable): myKaarma recommended work for used inventory. SQL Server 2014 compatible.
-- Run in SSMS against the database that holds Current_Inventory. `uv run mykaarma load` fills the tables.
-- Current state only: each load replaces a VIN's rows; VINs that leave used inventory are pruned on a full run.
-- Customer identifiers are never stored; employee identifiers are (inspection_associates, approver/last_updated_by).

if object_id('dbo.src_mykaarma__vin_lookups', 'U') is null
create table dbo.src_mykaarma__vin_lookups (
	vin char(17) not null primary key,
	stock_no nvarchar(20), dealer_code varchar(10),
	checked_at datetime2(0) not null,              -- UTC
	departments_checked smallint, departments_failed smallint,
	inspections_found int,                         -- from the last error-free check
	error nvarchar(400)                            -- null = last check succeeded
);

if object_id('dbo.src_mykaarma__inspections', 'U') is null
create table dbo.src_mykaarma__inspections (
	mpi_uuid varchar(64) not null primary key,
	vin char(17) not null,
	store nvarchar(100), department_uuid char(64),
	order_uuid varchar(64), order_number varchar(20), order_date date, order_status varchar(4),
	mpi_status varchar(100),                       -- seen up to 52 chars
	mk_recommended_total decimal(12,2), mk_approved_total decimal(12,2),   -- myKaarma's own figures, for reconciliation
	mk_rec_count int, mk_approve_count int, mk_decline_count int,
	mpi_created_on datetime2(0), mpi_updated_on datetime2(0),             -- UTC
	loaded_at datetime2(0) not null,
	raw nvarchar(max)                              -- get-MPI detail JSON minus customerIdentifiers; storage only on 2014
);
if col_length('dbo.src_mykaarma__inspections', 'mpi_status') < 100       -- widen tables created before this fix
	alter table dbo.src_mykaarma__inspections alter column mpi_status varchar(100);
if not exists (select 1 from sys.indexes where name = 'ix_src_mykaarma__inspections_vin')
create index ix_src_mykaarma__inspections_vin on dbo.src_mykaarma__inspections (vin, order_date desc);

if object_id('dbo.src_mykaarma__recommendations', 'U') is null
create table dbo.src_mykaarma__recommendations (
	recommendation_uuid varchar(64) not null primary key,
	mpi_uuid varchar(64) not null,
	vin char(17) not null,
	section nvarchar(200), check_name nvarchar(400), description nvarchar(2000),
	severity varchar(6), severity_rank tinyint,    -- red 3, yellow 2, green 1, none 0 (worst colour on the check)
	status varchar(30), approval_state varchar(30),
	decision varchar(10) not null,                 -- approved / declined / pending
	amount decimal(12,2),                          -- null = not priced yet
	op_code varchar(50),
	approver_name nvarchar(200), last_updated_by nvarchar(200),
	rec_created_on datetime2(0), rec_updated_on datetime2(0),
	loaded_at datetime2(0) not null
);
if not exists (select 1 from sys.indexes where name = 'ix_src_mykaarma__recommendations_vin')
create index ix_src_mykaarma__recommendations_vin on dbo.src_mykaarma__recommendations (vin);
if not exists (select 1 from sys.indexes where name = 'ix_src_mykaarma__recommendations_mpi')
create index ix_src_mykaarma__recommendations_mpi on dbo.src_mykaarma__recommendations (mpi_uuid);

if object_id('dbo.src_mykaarma__inspection_associates', 'U') is null
create table dbo.src_mykaarma__inspection_associates (
	mpi_uuid varchar(64) not null,
	role varchar(20) not null,                     -- advisor / technician / assignee
	associate_uuid varchar(64) not null,
	associate_name nvarchar(200),
	primary key (mpi_uuid, role, associate_uuid)
);
go

if object_id('dbo.vw_mykaarma__vin_recommendation_summary', 'V') is not null
	drop view dbo.vw_mykaarma__vin_recommendation_summary;
go

-- One row per checked VIN; money and severity from its latest inspection. Amounts are line-item sums
-- (unpriced lines count as 0 and are counted in unpriced_count); mk_* are myKaarma's totals for comparison.
create view dbo.vw_mykaarma__vin_recommendation_summary as
with ranked as (
	select i.*,
		row_number() over (partition by i.vin order by i.order_date desc, i.mpi_created_on desc, i.mpi_uuid) as rn,
		count(*) over (partition by i.vin) as inspections_all_time
	from dbo.src_mykaarma__inspections i
), latest as (
	select * from ranked where rn = 1
), rec as (
	select r.mpi_uuid,
		count(*) as rec_count,
		sum(case when r.amount is null then 1 else 0 end) as unpriced_count,
		sum(isnull(r.amount, 0)) as recommended_amt,
		sum(case when r.decision = 'approved' then isnull(r.amount, 0) else 0 end) as approved_amt,
		sum(case when r.decision = 'declined' then isnull(r.amount, 0) else 0 end) as declined_amt,
		sum(case when r.decision = 'pending' then isnull(r.amount, 0) else 0 end) as pending_amt,
		sum(case when r.decision = 'approved' then 1 else 0 end) as approved_count,
		sum(case when r.decision = 'declined' then 1 else 0 end) as declined_count,
		sum(case when r.decision = 'pending' then 1 else 0 end) as pending_count,
		sum(case when r.severity = 'red' then 1 else 0 end) as red_count,
		sum(case when r.severity = 'yellow' then 1 else 0 end) as yellow_count,
		sum(case when r.severity = 'red' then isnull(r.amount, 0) else 0 end) as red_amt,
		max(r.severity_rank) as worst_rank
	from dbo.src_mykaarma__recommendations r
	group by r.mpi_uuid
), adv as (
	select mpi_uuid, max(associate_name) as advisor_name
	from dbo.src_mykaarma__inspection_associates
	where role = 'advisor'
	group by mpi_uuid
)
select
	v.vin, v.stock_no, v.dealer_code, v.checked_at, v.error,
	isnull(l.inspections_all_time, 0) as inspections_all_time,
	l.store, l.order_number, l.order_date, a.advisor_name,
	isnull(r.rec_count, 0) as rec_count, isnull(r.unpriced_count, 0) as unpriced_count,
	isnull(r.recommended_amt, 0) as recommended_amt, isnull(r.approved_amt, 0) as approved_amt,
	isnull(r.declined_amt, 0) as declined_amt, isnull(r.pending_amt, 0) as pending_amt,
	isnull(r.approved_count, 0) as approved_count, isnull(r.declined_count, 0) as declined_count,
	isnull(r.pending_count, 0) as pending_count,
	isnull(r.red_count, 0) as red_count, isnull(r.yellow_count, 0) as yellow_count, isnull(r.red_amt, 0) as red_amt,
	case r.worst_rank when 3 then 'red' when 2 then 'yellow' when 1 then 'green' end as worst_severity,
	l.mk_recommended_total, l.mk_approved_total
from dbo.src_mykaarma__vin_lookups v
left join latest l on l.vin = v.vin
left join rec r on r.mpi_uuid = l.mpi_uuid
left join adv a on a.mpi_uuid = l.mpi_uuid;
go
