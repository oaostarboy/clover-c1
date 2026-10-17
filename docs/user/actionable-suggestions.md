# Actionable suggestions

A suggestion is still optional advice. Feedback **accept** is not permission to run tools. A supported suggestion can additionally offer one exact action after the reply, with native **🔧 Do it / Not now** buttons. Only the authenticated person's explicit tap authorizes that action once. Suggestions do not create standing permission or enable automatic local fixes.

## First supported action: pause future runs of one failing job

The existing local workstream offer must pass its normal relevance, timing, cooldown, duplicate and per-session gates. Both the current question and that offer must name exactly one canonical job ID. The job must have persisted authenticated private workstream-owner metadata matching this profile, project and chat. Its last result must be an error, its current state must be scheduled/enabled, and its delivery must be local. Missing ownership, ambiguous IDs, other audiences, active run/fire claims and unknown state do not receive an executable offer.

**Do it** pauses only future runs of that exact unchanged job through the ordinary `cronjob(action="pause")` runtime middleware and job owner. It does not cancel a current run, create/run another job, contact a model, send data, delete anything or repair the job's failure. Ordinary requested work keeps its existing permission flow; discovery limits do not add confirmations to it.

This first native surface is Telegram's existing owned picker. Other surfaces without that transport remain advice, not pretend working buttons. No command text, mail, tool result, model output, old feedback or fabricated action JSON can create an accepted callback capability.

## Safety and evidence

The runtime captures the authenticated original task/receipt, exact profile/project/actor/chat/session route, original tool/approval policy, remaining task budget, job snapshot and five-minute expiry. The picker additionally binds adapter identity/credentials, message and thread. A new task or Stop invalidates an old offer. Permission, managed policy, target and stop checks run again before dispatch and under the job owner's existing lock. Middleware can refuse the action but cannot rewrite it into another action. One token has at most one execution attempt; unknown outcomes never trigger automatic retry. A restart of Clover discards pending offers.

The existing receipt ledger distinguishes **proposed**, **requested**, **started**, **completed**, **verified**, **failed** and **unknown** in bounded action-receipt detail. Authorization is recorded before dispatch. Completion requires the job owner's guarded mutation/readback receipt, not a success-looking tool string. Verification then reads the exact target again and permits only the expected pause lifecycle fields to differ. Paused status is not proof that the original failure was repaired. Readback failures remain unknown.

**Not now** records ordinary deferred suggestion feedback without execution authority. Existing feedback Stop/reject/done and fresh source/policy revocation prevent execution. Invalid/foreign taps cannot consume another person's valid choice.

Managed profiles require the shared fresh strict managed-policy reader supplied by the separate M1 correction. Before that dependency is integrated, configured managed scope refuses actionable approvals rather than using the forgiving startup cache. Unconfigured scope works without that dependency. Installing this change changes no preference or permission.

## Unsupported actions are not made safe by a button

Application restart, including Bambu Studio, has no adapter here. Clover cannot prove the application's identity, absence of an active print/job, or absence of unsaved work through this path. It must not present an executable restart button or claim that restarting fixes lag. A separately implemented bounded adapter and renewed exact approval are required; unknown print/work state must remain a blocker. This feature does not promise a universal unsaved-work detector.

Sending, spending, deleting, arbitrary shell plans, app control and enabling future schedules are also outside this first action type. Their existing permissions and approval systems are unchanged. No new scheduler, model tool, paid classifier, prompt/memory instruction or automatic-fix setting is added.
