from fastapi import HTTPException, Query

from actionflow.config import Settings
from actionflow.operations import run_diagnostics, service_diagnostics


def register_diagnostics_routes(router, Db, session_required, settings: Settings):
    @router.get("/diagnostics", dependencies=[session_required])
    def diagnostics(
        probe_provider: bool = Query(False),
        db=Db,
    ):
        return service_diagnostics(
            db, settings, probe_provider=probe_provider
        )

    @router.get(
        "/analyses/{run_id}/diagnostics",
        dependencies=[session_required],
    )
    def analysis_diagnostics(
        run_id: str,
        probe_provider: bool = Query(False),
        db=Db,
    ):
        result = run_diagnostics(
            db, run_id, settings, probe_provider=probe_provider
        )
        if result is None:
            raise HTTPException(404, "Запуск не найден")
        return result
