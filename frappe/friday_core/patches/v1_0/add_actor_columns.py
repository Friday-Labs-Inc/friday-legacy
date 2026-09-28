"""Design 99 — put the actor columns on tables that already exist.

`_actor_kind`, `_actor` and `_trace_id` are optional columns (like `_comments`),
so Frappe adds them when a DocType's table is created or altered. Tables that
already existed and never changed would never get them — and a stamp that lands
on some tables and not others is worse than none. This patch adds them once, to
every non-child table, with the database's own IF NOT EXISTS so a re-run is a
no-op.

Child tables are skipped: rows there belong to their parent, and the parent
carries the stamp.
"""

import frappe

COLUMNS = (("_actor_kind", 32), ("_actor", 140), ("_trace_id", 64))


def execute():
	parents = {
		f"tab{name}"
		for name in frappe.get_all("DocType", filters={"istable": 0, "is_virtual": 0}, pluck="name")
	}
	existing = set(frappe.db.get_tables(cached=False))
	added = 0
	for table in sorted(parents & existing):
		for column, length in COLUMNS:
			try:
				frappe.db.savepoint("friday_actor_column")
				frappe.db.sql_ddl(
					f"ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS `{column}` varchar({length}) NULL"
					if frappe.db.db_type != "postgres"
					else f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column}" varchar({length}) NULL'
				)
				added += 1
			except Exception:
				frappe.db.rollback(save_point="friday_actor_column")
				frappe.logger("friday.actor").warning(f"could not add {column} to {table}", exc_info=True)
	frappe.db.commit()
	print(f"actor columns: {added} column(s) ensured across {len(parents & existing)} tables")
