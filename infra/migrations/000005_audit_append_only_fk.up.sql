-- 000005_audit_append_only_fk — reconcile the admin_audit_log append-only
-- trigger with the table's own FK actions.
--
-- Bug: actor_user_id / target_user_id are declared ON DELETE SET NULL, so
-- deleting a user issues an UPDATE on admin_audit_log to null those columns.
-- The original BEFORE UPDATE trigger raised unconditionally, so any user with
-- audit rows could NOT be deleted — GDPR-style erasure failed outright.
--
-- Fix: permit ONLY the FK SET-NULL cleanup (actor_user_id / target_user_id
-- transitioning to NULL). Every content column must stay unchanged, the FK
-- columns may only move toward NULL (never be re-pointed), and DELETE remains
-- forbidden. The log therefore stays append-only for its substance while the
-- referential cleanup the schema itself requests is allowed through.

CREATE OR REPLACE FUNCTION prevent_admin_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        -- No content column may change.
        IF ROW(NEW.id, NEW.action, NEW.reason, NEW.before_state,
               NEW.after_state, NEW.created_at)
           IS DISTINCT FROM
           ROW(OLD.id, OLD.action, OLD.reason, OLD.before_state,
               OLD.after_state, OLD.created_at)
        THEN
            RAISE EXCEPTION 'admin_audit_log is append-only';
        END IF;
        -- The actor / target references may only be cleared (FK SET NULL),
        -- never changed to a different user.
        IF (NEW.actor_user_id IS NOT NULL
                AND NEW.actor_user_id IS DISTINCT FROM OLD.actor_user_id)
           OR (NEW.target_user_id IS NOT NULL
                AND NEW.target_user_id IS DISTINCT FROM OLD.target_user_id)
        THEN
            RAISE EXCEPTION 'admin_audit_log is append-only';
        END IF;
        RETURN NEW;
    END IF;
    -- DELETE (and anything else) stays forbidden.
    RAISE EXCEPTION 'admin_audit_log is append-only';
END;
$$;
