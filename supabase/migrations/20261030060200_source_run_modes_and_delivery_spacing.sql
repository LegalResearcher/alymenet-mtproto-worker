CREATE TABLE IF NOT EXISTS public.alymenet_source_run_control (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton = true),
  mode text NOT NULL DEFAULT 'legacy' CHECK (mode IN ('legacy','latest_only','catch_up')),
  interval_minutes integer NOT NULL DEFAULT 0 CHECK (interval_minutes BETWEEN 0 AND 1440),
  cutoff_message_id bigint NOT NULL DEFAULT 0 CHECK (cutoff_message_id >= 0),
  next_allowed_at timestamptz NOT NULL DEFAULT '-infinity'::timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO public.alymenet_source_run_control(singleton)
VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

ALTER TABLE public.alymenet_source_run_control ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.alymenet_source_run_control FROM anon, authenticated;
GRANT ALL ON public.alymenet_source_run_control TO service_role;

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
  v_now timestamptz := clock_timestamp();
BEGIN
  IF p_message_id IS NULL OR p_message_id <= 0 THEN RAISE EXCEPTION 'Invalid source message ID'; END IF;
  IF p_path NOT IN ('publisher', 'mtproto') THEN RAISE EXCEPTION 'Invalid delivery path'; END IF;
  IF p_owner IS NULL THEN RAISE EXCEPTION 'Missing owner token'; END IF;

  SELECT mode, cutoff_message_id, interval_minutes, next_allowed_at
    INTO v_mode, v_cutoff, v_interval, v_next_allowed
    FROM public.alymenet_source_run_control WHERE singleton = true FOR UPDATE;
  IF NOT FOUND THEN
    v_mode := 'legacy'; v_cutoff := 0; v_interval := 0; v_next_allowed := '-infinity'::timestamptz;
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

DO $schedule$
DECLARE j record;
BEGIN
  FOR j IN SELECT jobid FROM cron.job WHERE jobname = 'alymenet-mtproto-github-every-5-minutes' LOOP
    PERFORM cron.alter_job(j.jobid, schedule => '* * * * *');
  END LOOP;
END
$schedule$;
