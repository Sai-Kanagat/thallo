// The Thallo mark from the public page: a tile, a leaf and its vein. Colours come from CSS so the
// tile inverts in dark mode and the leaf stays cobalt.
export function Mark({ className = "brand-mark" }: { className?: string }) {
  return (
    <svg viewBox="0 0 100 100" aria-hidden="true" className={className}>
      <rect className="tile" x="4" y="4" width="92" height="92" rx="6" />
      <path className="leaf" d="M18 82 C18 44 44 18 82 18 C82 56 56 82 18 82 Z" />
      <line className="vein" x1="18" y1="82" x2="82" y2="18" />
    </svg>
  );
}
