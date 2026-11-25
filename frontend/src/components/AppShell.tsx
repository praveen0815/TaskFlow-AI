import { Outlet } from "react-router-dom";

export default function AppShell() {
  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800">
        <div className="mx-auto flex max-w-6xl items-center gap-3 px-6 py-5">
          <div className="grid size-9 place-items-center rounded-lg bg-cyan-400 font-bold text-slate-950">
            T
          </div>
          <div>
            <p className="font-semibold tracking-wide">TaskFlow AI</p>
            <p className="text-xs text-slate-400">Workflow orchestration</p>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-12">
        <Outlet />
      </main>
    </div>
  );
}
