#!/usr/bin/env python3
"""Trusted script-only inbox watcher entry, invoked by the existing scheduler.

It needs the current gateway's admitted native adapters for authority and verified
private delivery. It never launches a scheduling agent or emits private stdout.
Standalone execution is intentionally unavailable, not an unguarded fallback.
"""


def run_owned(job, *, adapters=None, loop=None):
    from clover_constants import get_clover_home
    from cron.inbox_watcher import Store, run, state_path
    from gateway.inbox_watcher_controls import binding_for_job
    try:
        binding = binding_for_job(job, adapters, loop)
        run(job, binding)
    except Exception:
        # Never echo source/provider exception strings or private content.
        try:
            if job.get('inbox_watcher', {}).get('home') == str(get_clover_home().resolve()):
                with Store(state_path(get_clover_home())).edit() as state:
                    state['status'] = 'current live native gateway authority unavailable'
        except Exception:
            pass
    return True, '', '', None


if __name__ == '__main__':
    raise SystemExit('Run this owned watcher through the native gateway scheduler.')
