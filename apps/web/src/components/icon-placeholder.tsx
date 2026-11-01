import { Check, ChevronDown, ChevronUp, X } from "lucide-react"
import type { SVGProps } from "react"

const ICONS = {
  CheckIcon: Check,
  ChevronDownIcon: ChevronDown,
  ChevronUpIcon: ChevronUp,
  XIcon: X,
} as const

export function IconPlaceholder({
  lucide,
  tabler,
  hugeicons,
  phosphor,
  remixicon,
  ...svgProps
}: {
  lucide?: string
  tabler?: string
  hugeicons?: string
  phosphor?: string
  remixicon?: string
} & SVGProps<SVGSVGElement>) {
  void tabler
  void hugeicons
  void phosphor
  void remixicon
  const Icon = lucide && lucide in ICONS ? ICONS[lucide as keyof typeof ICONS] : undefined
  if (!Icon) return null
  return <Icon {...svgProps} />
}
