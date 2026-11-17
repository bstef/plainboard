import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { qk, useAuth, useSettings } from "./api/queries";
import { Spinner } from "./components/ui";
import { CreateAdmin, Login } from "./features/auth/Login";
import { Shell } from "./features/layout/Shell";
import { SetupWizard } from "./features/onboarding/SetupWizard";

export default function App() {
  const auth = useAuth();
  const qc = useQueryClient();

  useEffect(() => {
    const onUnauthorized = () => qc.invalidateQueries({ queryKey: qk.auth });
    window.addEventListener("plainboard:unauthorized", onUnauthorized);
    return () => window.removeEventListener("plainboard:unauthorized", onUnauthorized);
  }, [qc]);

  if (auth.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner className="size-6" />
      </div>
    );
  }
  if (auth.data?.setup_required) return <CreateAdmin status={auth.data} />;
  if (auth.data && !auth.data.user) return <Login />;
  return <SignedIn admin={!!auth.data?.user?.is_admin} />;
}

/** A new installation's admin goes through the basic settings once, before anything else. */
function SignedIn({ admin }: { admin: boolean }) {
  const settings = useSettings();
  if (admin && settings.data && !settings.data.values.setup_done) return <SetupWizard />;
  return <Shell />;
}
