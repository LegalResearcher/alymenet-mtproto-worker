CREATE OR REPLACE FUNCTION public.alymenet_delivery_transition(p_message_id bigint, p_path text, p_owner uuid, p_status text, p_target_message_id bigint DEFAULT NULL::bigint, p_detail text DEFAULT NULL::text)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
  v_rows integer;
  v_now timestamptz := clock_timestamp();
BEGIN
  IF p_message_id IS NULL OR p_message_id <= 0 THEN RAISE EXCEPTION 'Invalid source message ID'; END IF;
  IF p_path IS NULL OR p_path NOT IN ('publisher', 'mtproto') THEN RAISE EXCEPTION 'Invalid delivery path'; END IF;
  IF p_owner IS NULL THEN RAISE EXCEPTION 'Missing owner token'; END IF;
  IF p_status IS NULL OR p_status NOT IN ('sent', 'mtproto_handoff', 'skipped', 'uncertain', 'retryable') THEN RAISE EXCEPTION 'Invalid ledger transition'; END IF;
  IF p_path = 'publisher' AND p_status NOT IN ('sent', 'skipped', 'uncertain', 'retryable') THEN RAISE EXCEPTION 'Invalid publisher transition'; END IF;
  IF p_path = 'mtproto' AND p_status NOT IN ('mtproto_handoff', 'skipped', 'uncertain', 'retryable') THEN RAISE EXCEPTION 'Invalid MTProto transition'; END IF;

  UPDATE public.alymenet_delivery_ledger
  SET status = p_status,
      claimed_by = p_path,
      claim_token = NULL,
      target_message_id = COALESCE(p_target_message_id, target_message_id),
      detail = LEFT(p_detail, 2000),
      updated_at = v_now
  WHERE source_channel = 'ALYEMENNET' AND source_message_id = p_message_id AND target_channel = '@hasadalyoum'
    AND status = 'processing' AND claimed_by = p_path AND claim_token = p_owner;
  GET DIAGNOSTICS v_rows = ROW_COUNT;

  IF v_rows = 1 AND p_status IN ('sent', 'mtproto_handoff') THEN
    UPDATE public.alymenet_source_run_control
    SET next_allowed_at = CASE WHEN interval_minutes > 0
          THEN v_now + make_interval(mins => interval_minutes)
          ELSE '-infinity'::timestamptz END,
        updated_at = v_now
    WHERE singleton = true;
  END IF;
  RETURN v_rows = 1;
END;
$function$;

REVOKE ALL ON FUNCTION public.alymenet_delivery_transition(bigint, text, uuid, text, bigint, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.alymenet_delivery_transition(bigint, text, uuid, text, bigint, text) TO service_role, postgres;
