-- Adds the Season column to the product_categorisation source tables that lack it
-- (2026-11-17). product_categorisation and product_categorisation_us_ca_uk_it already
-- have it. Nullable, added as the last column: existing rows get NULL and
-- existing INSERTs that name their columns keep working.
-- Undo: ALTER TABLE <table> DROP COLUMN <column>;
ALTER TABLE powerbidb.product_categorisation_gr     ADD COLUMN `Season` VARCHAR(25) NULL;
ALTER TABLE powerbidb.product_categorisation_uk     ADD COLUMN `Season` VARCHAR(25) NULL;
ALTER TABLE powerbidb.product_categorisation_non_us ADD COLUMN `season` VARCHAR(25) NULL;  -- snake_case like its other columns
