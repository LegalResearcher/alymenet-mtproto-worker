ALTER TABLE public.alymenet_source_run_control
  ADD COLUMN IF NOT EXISTS pause_until timestamptz,
  ADD COLUMN IF NOT EXISTS resume_mode text CHECK (resume_mode IS NULL OR resume_mode IN ('latest_only','catch_up'));

CREATE OR REPLACE FUNCTION public.alymenet_delivery_claim(p_message_id bigint, p_path text, p_owner uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
  v_status text;
  v_exists boolean := false;
  v_mode text;
  v_cutoff bigint;
  v_interval integer;
  v_next_allowed timestamptz;
  v_pause_until timestamptz;
  v_resume_mode text;
  v_now timestamptz := clock_timestamp();
BEGIN
  IF p_message_id IS NULL OR p_message_id <= 0 THEN RAISE EXCEPTION 'Invalid source message ID'; END IF;
  IF p_path NOT IN ('publisher', 'mtproto') THEN RAISE EXCEPTION 'Invalid delivery path'; END IF;
  IF p_owner IS NULL THEN RAISE EXCEPTION 'Missing owner token'; END IF;

  SELECT mode, cutoff_message_id, interval_minutes, next_allowed_at, pause_until, resume_mode
    INTO v_mode, v_cutoff, v_interval, v_next_allowed, v_pause_until, v_resume_mode
    FROM public.alymenet_source_run_control WHERE singleton = true FOR UPDATE;
  IF NOT FOUND THEN
    v_mode := 'legacy'; v_cutoff := 0; v_interval := 0;
    v_next_allowed := '-infinity'::timestamptz; v_pause_until := NULL; v_resume_mode := NULL;
  END IF;

  IF v_pause_until IS NOT NULL AND v_pause_until > v_now THEN
    IF v_resume_mode = 'latest_only' THEN
      UPDATE public.alymenet_source_run_control
      SET cutoff_message_id = GREATEST(cutoff_message_id, p_message_id), updated_at = v_now
      WHERE singleton = true;
    END IF;
    RETURN jsonb_build_object('acquired', false, 'status', 'paused', 'resume_at', v_pause_until);
  END IF;

  IF v_pause_until IS NOT NULL AND v_pause_until <= v_now THEN
    IF v_resume_mode = 'latest_only' THEN
      v_mode := 'latest_only'; v_interval := 0;
      UPDATE public.alymenet_source_run_control
      SET mode = 'latest_only', interval_minutes = 0, pause_until = NULL, resume_mode = NULL,
          next_allowed_at = '-infinity'::timestamptz, updated_at = v_now
      WHERE singleton = true;
    ELSIF v_resume_mode = 'catch_up' THEN
      v_mode := 'catch_up'; v_interval := 1;
      UPDATE public.alymenet_source_run_control
      SET mode = 'catch_up', interval_minutes = 1, pause_until = NULL, resume_mode = NULL,
          next_allowed_at = '-infinity'::timestamptz, updated_at = v_now
      WHERE singleton = true;
    ELSE
      UPDATE public.alymenet_source_run_control
      SET pause_until = NULL, resume_mode = NULL, updated_at = v_now
      WHERE singleton = true;
    END IF;
    SELECT cutoff_message_id, next_allowed_at INTO v_cutoff, v_next_allowed
      FROM public.alymenet_source_run_control WHERE singleton = true;
  END IF;

  IF v_mode = 'latest_only' AND p_message_id <= v_cutoff THEN
    RETURN jsonb_build_object('acquired', false, 'status', 'skipped_before_cutoff');
  END IF;

  SELECT status INTO v_status
    FROM public.alymenet_delivery_ledger
    WHERE source_channel = 'ALYEMENNET' AND source_message_id = p_message_id AND target_channel = '@hasadalyoum'
    FOR UPDATE;
  v_exists := FOUND;
  IF v_exists AND v_status <> 'retryable' THEN
    RETURN jsonb_build_object('acquired', false, 'status', COALESCE(v_status, 'missing'));
  END IF;
  IF v_interval > 0 AND v_next_allowed > v_now THEN
    RETURN jsonb_build_object('acquired', false, 'status', 'interval_wait',
      'retry_after_seconds', GREATEST(1, CEIL(EXTRACT(EPOCH FROM (v_next_allowed - v_now)))::integer));
  END IF;

  IF v_exists THEN
    UPDATE public.alymenet_delivery_ledger
    SET status = 'processing', claimed_by = p_path, claim_token = p_owner,
        detail = NULL, attempt_count = attempt_count + 1, updated_at = v_now
    WHERE source_channel = 'ALYEMENNET' AND source_message_id = p_message_id
      AND target_channel = '@hasadalyoum' AND status = 'retryable';
    IF NOT FOUND THEN RETURN jsonb_build_object('acquired', false, 'status', 'busy'); END IF;
  ELSE
    INSERT INTO public.alymenet_delivery_ledger
      (source_channel, source_message_id, target_channel, status, claimed_by, claim_token)
    VALUES ('ALYEMENNET', p_message_id, '@hasadalyoum', 'processing', p_path, p_owner)
    ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN
      SELECT status INTO v_status FROM public.alymenet_delivery_ledger
      WHERE source_channel = 'ALYEMENNET' AND source_message_id = p_message_id AND target_channel = '@hasadalyoum';
      RETURN jsonb_build_object('acquired', false, 'status', COALESCE(v_status, 'busy'));
    END IF;
  END IF;

  UPDATE public.alymenet_source_run_control
  SET next_allowed_at = CASE WHEN v_interval > 0
        THEN v_now + make_interval(mins => v_interval)
        ELSE '-infinity'::timestamptz END,
      updated_at = v_now
  WHERE singleton = true;
  RETURN jsonb_build_object('acquired', true, 'status', 'processing');
END;
$function$;

REVOKE ALL ON FUNCTION public.alymenet_delivery_claim(bigint, text, uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.alymenet_delivery_claim(bigint, text, uuid) TO service_role, postgres;
