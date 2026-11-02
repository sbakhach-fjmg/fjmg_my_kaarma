-- Used vehicles in stock: fj-internal-allocation batch/sql/transfer_candidates.sql filters minus its
-- [User Status] = 'W', so every used car in stock is covered, not just transfer candidates.
-- One row per VIN (a car with two stock records keeps the most recently received one).
with c as (
select
	upper(ltrim(rtrim(vin))) as vin,
	[Stock#] as stock_no,
	upper(ltrim(rtrim(StoreID))) as dealer_code,
	row_number() over (partition by upper(ltrim(rtrim(vin))) order by try_convert(date, [Received Date], 1) desc) as rn
from Current_Inventory
where [Veh Status] = 'P'
  and [New/Used] = 'U'
  and len(ltrim(rtrim(vin))) = 17
  and StoreID not in ('FRMBN', 'FRPOR', 'AUDFR', 'WCPOR')
)
select vin, stock_no, dealer_code
from c
where rn = 1
order by vin;
