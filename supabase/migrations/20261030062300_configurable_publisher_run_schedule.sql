ALTER TABLE public.alymenet_source_run_control
  ADD COLUMN IF NOT EXISTS schedule_minutes integer
  CHECK (schedule_minutes IS NULL OR schedule_minutes BETWEEN 1 AND 10080);

CREATE OR REPLACE FUNCTION public.alymenet_set_schedule_minutes(p_minutes integer)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public', 'cron'
AS $function$
DECLARE
  j record;
  v_count integer := 0;
  v_schedule text;
BEGIN
  IF p_minutes IS NULL OR p_minutes < 1 OR p_minutes > 10080 THEN
    RAISE EXCEPTION 'Schedule interval must be between 1 and 10080 minutes';
  END IF;
  v_schedule := format('%s minutes', p_minutes);
  FOR j IN
    SELECT jobid FROM cron.job
    WHERE jobname IN ('alymenet-test-every-minute', 'alymenet-mtproto-github-every-5-minutes')
  LOOP
    PERFORM cron.alter_job(j.jobid, schedule => v_schedule);
    v_count := v_count + 1;
  END LOOP;
  IF v_count = 0 THEN RAISE EXCEPTION 'No ALYEMENNET publishing jobs were found'; END IF;
  UPDATE public.alymenet_source_run_control
  SET schedule_minutes = p_minutes, updated_at = clock_timestamp()
  WHERE singleton = true;
  RETURN v_count;
END;
$function$;

REVOKE ALL ON FUNCTION public.alymenet_set_schedule_minutes(integer) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.alymenet_set_schedule_minutes(integer) TO service_role, postgres;
