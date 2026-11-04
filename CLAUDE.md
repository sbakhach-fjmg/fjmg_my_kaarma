# Rules

## Never touch customer PII (unbreakable)

Never select or store customer PII, in any query, script, table or file, for any reason. If a task seems to need it,
stop and ask.

- Name every column. No `select *` or `select top N *` from a table or view that holds or may hold customer data:
  `Current_Inventory`, `FactSales`, `PendingDeals`, `DeletedSales`, `FactVehicleInventory`, `dbo.src_mykaarma__*`.
- Off limits, also in `where`, `join` and `group by`: `Customer*`, `CoBuyer*`, `Co-Buyers*`, `BoBuyer*` (names,
  address, city, state, zip, county, phones, email, DOB), `CustomerID_Advent`, `fkCustomerKey`, `CustomerKey`,
  `BuyerFico`, `Business Name`, `License ##`, `Insurance*`, `Bank*` address fields, `*Lienholder/Payoff` fields.
- myKaarma payloads: customer identifiers are stripped before storing (see README); keep it that way.
- Column names from `sys.columns` are fine to read; their values are not.
- Employee names (advisor, technician, salesperson) are not customer PII.
