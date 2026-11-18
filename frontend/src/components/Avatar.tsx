import clsx from "clsx";
import type { Person } from "../api/types";

/** Where a person's profile picture is. The version is part of the address: a new picture is seen at once. */
export function avatarUrl(person: Pick<Person, "id" | "avatar_version">): string {
  return `/api/avatars/${person.id}?v=${person.avatar_version}`;
}

/** A person, round: their profile picture, or their initial on a colour of their own. Agents are squares. */
export function Avatar({ name, person, className }: { name: string; person?: Pick<Person, "id" | "avatar_version"> | null; className?: string }) {
  const hue = hueOf(name);
  return (
    <span
      data-part="avatar"
      title={name}
      className={clsx("inline-flex size-5 shrink-0 items-center justify-center overflow-hidden rounded-full text-[0.6875rem] leading-none font-bold select-none", className)}
      style={person?.avatar_version ? undefined : { background: `oklch(0.87 0.07 ${hue})`, color: `oklch(0.36 0.09 ${hue})` }}
    >
      {person?.avatar_version ? <img src={avatarUrl(person)} alt="" className="size-full object-cover" /> : (name.trim()[0]?.toUpperCase() ?? "?")}
    </span>
  );
}

/** The same name always gets the same colour. */
function hueOf(name: string): number {
  let sum = 0;
  for (const ch of name.trim().toLowerCase()) sum = (sum * 31 + ch.codePointAt(0)!) % 360;
  return sum;
}

/** A chosen image as a small square: cut from its middle, so nothing large is ever sent or stored. */
export async function squarePicture(file: File, size = 256): Promise<Blob> {
  const image = await createImageBitmap(file);
  const side = Math.min(image.width, image.height);
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  canvas.getContext("2d")!.drawImage(image, (image.width - side) / 2, (image.height - side) / 2, side, side, 0, 0, size, size);
  image.close();
  // A browser that cannot write WebP answers with a PNG, which is fine at this size.
  return new Promise((resolve, reject) => canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("no picture"))), "image/webp", 0.86));
}
