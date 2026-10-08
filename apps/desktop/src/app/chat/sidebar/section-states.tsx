import { CloverMascot } from '@/components/chat/clover-mascot'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Skeleton } from '@/components/ui/skeleton'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { useRootDecor } from '@/themes/root-decor'

import { SidebarRowCluster, SidebarRowShell, SidebarRowStack } from './chrome'

// Stands in for session rows, so it borrows their chrome instead of copying
// the grid — a placeholder on a different edge than the rows it resolves into
// makes the list step sideways on load.
export function SidebarSessionSkeletons() {
  return (
    <SidebarRowStack aria-hidden="true">
      {['w-32', 'w-40', 'w-28', 'w-36', 'w-24'].map((width, i) => (
        <SidebarRowShell actions={<Skeleton className="size-3.5 rounded-sm opacity-60" />} key={`${width}-${i}`}>
          <SidebarRowCluster>
            <Skeleton className={cn('h-3 rounded-sm', width)} />
          </SidebarRowCluster>
        </SidebarRowShell>
      ))}
    </SidebarRowStack>
  )
}

export function SidebarBlankState({
  onNewProject,
  onNewSession
}: {
  onNewProject: () => void
  onNewSession?: () => void
}) {
  const { t } = useI18n()
  const s = t.sidebar
  const decor = useRootDecor()

  // Clover decor: the mascot, a title that says what this area is for, and the
  // action a new user actually wants (a chat) ahead of the project one.
  if (decor === 'clover') {
    const c = t.cloverHome

    return (
      <div className="grid min-h-0 flex-1 place-items-center px-4 text-center" data-slot="sidebar-blank-state">
        <div className="flex flex-col items-center gap-1.5">
          <CloverMascot className="mb-1 opacity-90" size={56} still />
          <p className="text-[0.8125rem] font-medium text-(--ui-text-secondary)">{c.sidebarEmptyTitle}</p>
          <p className="max-w-[13rem] text-xs leading-4.5 text-(--ui-text-tertiary)">{c.sidebarEmptyBody}</p>
          <div className="mt-2 flex flex-col items-center gap-1">
            {onNewSession ? (
              <Button onClick={onNewSession} size="sm" variant="secondary">
                <Codicon name="add" size="0.75rem" />
                {c.sidebarStartChat}
              </Button>
            ) : null}
            <Button className="text-(--ui-text-tertiary)" onClick={onNewProject} size="sm" variant="ghost">
              <Codicon name="root-folder" size="0.75rem" />
              {s.projects.newButton}
            </Button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="grid min-h-0 flex-1 place-items-center px-4 text-center">
      <div className="flex flex-col items-center gap-2">
        <Codicon className="text-(--ui-text-quaternary)" name="root-folder" size="1.25rem" />
        <p className="text-xs text-(--ui-text-tertiary)">{s.noSessions}</p>
        <Button className="mt-0.5 text-(--ui-text-secondary)" onClick={onNewProject} size="sm" variant="ghost">
          <Codicon name="add" size="0.75rem" />
          {s.projects.newButton}
        </Button>
      </div>
    </div>
  )
}

export function SidebarPinnedEmptyState() {
  const { t } = useI18n()

  return (
    <div className="flex min-h-7 items-center gap-1.5 rounded-lg pl-2 text-[0.75rem] text-(--ui-text-tertiary)">
      <span className="grid w-3.5 shrink-0 place-items-center text-(--ui-text-quaternary)">
        <Codicon name="pin" size="0.75rem" />
      </span>
      <span>{t.sidebar.shiftClickHint}</span>
    </div>
  )
}
