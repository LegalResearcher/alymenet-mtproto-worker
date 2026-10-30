CREATE OR REPLACE FUNCTION public.alymenet_reset_schedule()
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
  FOR j IN
    SELECT jobid, jobname FROM cron.job
    WHERE jobname IN ('alymenet-test-every-minute', 'alymenet-mtproto-github-every-5-minutes')
  LOOP
    v_schedule := CASE
      WHEN j.jobname = 'alymenet-test-every-minute' THEN '15 seconds'
      ELSE '*/5 * * * *'
    END;
    PERFORM cron.alter_job(j.jobid, schedule => v_schedule);
    v_count := v_count + 1;
  END LOOP;
  IF v_count = 0 THEN RAISE EXCEPTION 'No ALYEMENNET publishing jobs were found'; END IF;
  UPDATE public.alymenet_source_run_control
  SET schedule_minutes = NULL, updated_at = clock_timestamp()
  WHERE singleton = true;
  RETURN v_count;
END;
$function$;
REVOKE ALL ON FUNCTION public.alymenet_reset_schedule() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.alymenet_reset_schedule() TO service_role, postgres;
