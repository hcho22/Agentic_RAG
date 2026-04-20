import { useRef, useState, type DragEvent } from 'react'
import { cn } from '@/lib/utils'
import { ACCEPTED_EXTENSIONS } from '@/lib/ingestion'

type Props = {
  onFiles: (files: File[]) => void
  disabled?: boolean
}

export function DropZone({ onFiles, disabled }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)

  function handleDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault()
    setDragging(false)
    if (disabled) return
    const files = Array.from(e.dataTransfer.files ?? [])
    if (files.length > 0) onFiles(files)
  }

  function handleDragOver(e: DragEvent<HTMLDivElement>) {
    e.preventDefault()
    if (!disabled) setDragging(true)
  }

  function handleDragLeave(e: DragEvent<HTMLDivElement>) {
    e.preventDefault()
    setDragging(false)
  }

  function handleBrowseClick() {
    if (!disabled) inputRef.current?.click()
  }

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={handleBrowseClick}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          handleBrowseClick()
        }
      }}
      onDrop={handleDrop}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      className={cn(
        'flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed px-6 py-12 text-center transition-colors',
        dragging
          ? 'border-neutral-400 bg-neutral-900'
          : 'border-neutral-700 bg-neutral-950 hover:border-neutral-500',
        disabled && 'cursor-not-allowed opacity-60',
      )}
    >
      <p className="text-sm font-medium text-neutral-200">
        Drag and drop files here, or click to browse
      </p>
      <p className="mt-1 text-xs text-neutral-500">
        Accepted: {ACCEPTED_EXTENSIONS.join(', ')} — PDFs and DOCX come in Module 5.
      </p>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={ACCEPTED_EXTENSIONS.join(',')}
        className="hidden"
        onChange={(e) => {
          const files = Array.from(e.target.files ?? [])
          if (files.length > 0) onFiles(files)
          e.target.value = ''
        }}
      />
    </div>
  )
}
