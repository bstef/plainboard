import { useRef } from "react";
import { useTranslation } from "react-i18next";
import type { Person } from "../api/types";
import { Avatar } from "./Avatar";
import { Button } from "./ui";

/** A person's picture with its buttons. ``onPick`` gets the chosen file as it is; null: remove the picture. */
export function PictureField({ person, name, hint, onPick }: { person: Pick<Person, "id" | "avatar_version">; name: string; hint: string; onPick: (file: File | null) => void }) {
  const { t } = useTranslation();
  const picker = useRef<HTMLInputElement>(null);
  return (
    <div className="flex items-center gap-3">
      <Avatar name={name} person={person} className="size-14 text-xl" />
      <div className="min-w-0">
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={() => picker.current?.click()}>
            {t(person.avatar_version ? "account.changePicture" : "account.addPicture")}
          </Button>
          {person.avatar_version > 0 && (
            <Button size="sm" variant="ghost" onClick={() => onPick(null)}>
              {t("account.removePicture")}
            </Button>
          )}
        </div>
        <p className="mt-1 text-fg-3">{hint}</p>
      </div>
      <input
        ref={picker}
        type="file"
        accept="image/*"
        hidden
        onChange={(e) => {
          const file = e.target.files?.[0];
          e.target.value = "";
          if (file) onPick(file);
        }}
      />
    </div>
  );
}
