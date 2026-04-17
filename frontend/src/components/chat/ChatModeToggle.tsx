import { cn } from '@/lib/utils'
import type { ChatMode } from '@/lib/chat'

type Props = {
  mode: ChatMode
  onChange: (mode: ChatMode) => void
  disabled?: boolean
}

const OPTIONS: Array<{ value: ChatMode; label: string; hint: string }> = [
  {
    value: 'responses',
    label: 'Responses',
    hint: "OpenAI's managed Responses API with file_search",
  },
  {
    value: 'completions',
    label: 'Completions',
    hint: 'Chat Completions API with our search_documents tool',
  },
]

export function ChatModeToggle({ mode, onChange, disabled }: Props) {
  return (
    <div
      role="radiogroup"
      aria-label="Chat mode"
      className="inline-flex items-center rounded-md border border-neutral-800 bg-neutral-900 p-0.5"
    >
      {OPTIONS.map((opt) => {
        const active = mode === opt.value
        return (
          <button
            key={opt.value}
            type="button"
            role="radio"
            aria-checked={active}
            title={opt.hint}
            onClick={() => onChange(opt.value)}
            disabled={disabled}
            className={cn(
              'rounded px-2.5 py-1 text-xs font-medium transition-colors',
              active
                ? 'bg-neutral-700 text-neutral-100'
                : 'text-neutral-400 hover:text-neutral-200',
              disabled && 'cursor-not-allowed opacity-60',
            )}
          >
            {opt.label}
          </button>
        )
      })}
    </div>
  )
}
