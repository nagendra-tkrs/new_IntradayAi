"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function AuthCallbackPage() {
  const router = useRouter();

  useEffect(() => {
    const hash = window.location.hash.substring(1);
    const params = new URLSearchParams(hash);
    const token = params.get("token");
    const state = params.get("state");
    const error = params.get("error");

    window.history.replaceState(null, "", window.location.pathname);

    if (error) {
      router.replace("/login?error=" + encodeURIComponent(error));
      return;
    }

    const savedState = sessionStorage.getItem("gstate");
    sessionStorage.removeItem("gstate");

    if (!token || !state || state !== savedState) {
      router.replace("/login?error=invalid_session");
      return;
    }

    localStorage.setItem("trading_token", token);
    router.replace("/");
  }, [router]);

  return (
    <div className="min-h-screen bg-[#0a0e17] flex items-center justify-center">
      <div className="text-center">
        <div className="w-10 h-10 border-4 border-blue-500/30 border-t-blue-500 rounded-full animate-spin mx-auto mb-4" />
        <p className="text-gray-400 text-sm">Completing sign in...</p>
      </div>
    </div>
  );
}
