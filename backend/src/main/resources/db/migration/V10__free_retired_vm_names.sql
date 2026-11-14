-- Retired rows used to keep their name, and uk_vms_name covers every row. A VM re-created on the board under the
-- same name (new vm_id) could then never be registered by POST /api/v1/vms/synchronizations (duplicate_name).
-- Retired rows now give their name back as <name>~<first 8 of vm_id> (VmRecord.retire); this frees the rows
-- retired before that change. 54 + 1 + 8 = 63, the name column limit.
UPDATE vms
SET name = CONCAT(LEFT(name, 54), '~', LEFT(vm_id, 8))
WHERE lifecycle = 'RETIRED';
