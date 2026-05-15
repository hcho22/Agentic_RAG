import { useEffect, type ReactNode } from 'react'
import { cn } from '@/lib/utils'

// Minimal modal primitive used by US-040 ShareDialog. No radix dep — the
// product needs a single dialog surface and we already custom-render a toast
// container the same way. Closes on backdrop click and Escape; the panel
// stops propagation so clicks inside the modal don't dismiss it.

type Props = {
  open: boolean
  onOpenChange: (open: boolean) => void
  title?: string
  description?: string
  children: ReactNode
  className?: string
}

export function Dialog({ open, onOpenChange, title, description, children, className }: Props) {
  useEffect(() => {
    if (!open) return
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onOpenChange(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onOpenChange])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      onClick={() => onOpenChange(false)}
    >
      <div
        className={cn(
          'w-full max-w-md rounded-lg border border-neutral-800 bg-neutral-950 shadow-xl',
          className,
        )}
        onClick={(e) => e.stopPropagation()}
      >
        {(title || description) && (
          <div className="border-b border-neutral-800 px-5 py-4">
            {title && <h3 className="text-base font-semibold text-neutral-100">{title}</h3>}
            {description && (
              <p className="mt-1 text-xs text-neutral-400">{description}</p>
            )}
          </div>
        )}
        <div className="px-5 py-4">{children}</div>
      </div>
    </div>
  )
}
