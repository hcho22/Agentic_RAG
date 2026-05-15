import { type HTMLAttributes } from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

// US-041: shadcn-style Badge primitive used by the granting-principal cue on
// retrieved chunks. Kept tiny — a single span with variants that match the
// existing button/toast aesthetic (dark surface, neutral text).

const badgeVariants = cva(
  'inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] font-medium leading-none',
  {
    variants: {
      variant: {
        default: 'border-neutral-700 bg-neutral-900 text-neutral-200',
        secondary: 'border-neutral-800 bg-neutral-900/60 text-neutral-300',
      },
    },
    defaultVariants: { variant: 'default' },
  },
)

export interface BadgeProps
  extends HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />
}
