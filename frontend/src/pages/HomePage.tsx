import { useEffect, useState } from "react";
import { getApiHealth } from "../lib/api";

type ApiStatus = "checking" | "available" | "unavailable";

export default function HomePage() {
  const [apiStatus, setApiStatus] = useState<ApiStatus>("checking");

  useEffect(() => {
    let active = true;
    getApiHealth()
      .then(() => {
        if (active) setApiStatus("available");
      })
      .catch(() => {
        if (active) setApiStatus("unavailable");
      });

    return () => {
      active = false;
    };
  }, []);

  return (
    <section className="max-w-3xl">
      <p className="mb-3 text-sm font-medium uppercase tracking-[0.2em] text-cyan-300">
        Foundation
      </p>
      <h1 className="text-4xl font-semibold tracking-tight sm:text-5xl">
        Your workflow workspace starts here.
      </h1>
      <p className="mt-5 max-w-2xl text-lg leading-8 text-slate-400">
        TaskFlow AI connects a typed API and web application. Workflow planning
        and execution are not enabled in this phase.
      </p>
      <div className="mt-10 inline-flex items-center gap-3 rounded-xl border border-slate-800 bg-slate-900 px-4 py-3">
        <span
          aria-hidden="true"
          className={`size-2.5 rounded-full ${
            apiStatus === "available"
              ? "bg-emerald-400"
              : apiStatus === "unavailable"
                ? "bg-rose-400"
                : "bg-amber-400"
          }`}
        />
        <span className="text-sm text-slate-300">
          API: {apiStatus === "checking" ? "checking" : apiStatus}
        </span>
      </div>
    </section>
  );
}
