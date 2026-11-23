import { Play } from "lucide-react";

/** A video as a picture: its first frame, muted, with a play mark. The click is the parent's. */
export function VideoThumb({
  src,
  className,
}: {
  src: string;
  className?: string;
}) {
  return (
    <span className="relative block size-full">
      <video
        src={`${src}#t=0.001`}
        muted
        playsInline
        preload="metadata"
        tabIndex={-1}
        className={className ?? "size-full object-cover"}
      />
      <span className="pointer-events-none absolute inset-0 flex items-center justify-center">
        <span className="flex size-8 items-center justify-center rounded-full bg-black/60 text-white">
          <Play className="size-4 fill-current" />
        </span>
      </span>
    </span>
  );
}
