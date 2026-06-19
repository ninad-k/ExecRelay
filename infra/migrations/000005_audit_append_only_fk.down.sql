-- Revert to the original strict trigger that forbids ALL updates and deletes.
-- NOTE: this reintroduces the erasure bug (deleting a user with audit rows
-- fails), so only roll back if you understand that consequence.

CREATE OR REPLACE FUNCTION prevent_admin_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'admin_audit_log is append-only';
END;
$$;
