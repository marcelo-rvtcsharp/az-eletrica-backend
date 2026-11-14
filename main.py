from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.models import Base, engine, migrar_colunas
from app.auth import exigir_admin
from app.routes import auth, eletricistas, obras, apontamentos, orcamentos, dashboard, financeiro, despesas, ferramentas, comprovantes, notificacoes, portal

# Cria as tabelas
Base.metadata.create_all(bind=engine)
migrar_colunas(engine)

app = FastAPI(
    title="AZ Elétrica — API",
    description="Sistema de gestão de equipes elétricas",
    version="1.0.0"
)

# CORS — permite acesso do PWA e do app mobile
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rotas
# auth e portal cuidam das próprias permissões; o resto é só do escritório (admin)
app.include_router(auth.router)
app.include_router(portal.router)
app.include_router(notificacoes.router)
_ADMIN = [Depends(exigir_admin)]
for r in (eletricistas, obras, apontamentos, orcamentos, dashboard, financeiro, despesas, ferramentas, comprovantes):
    app.include_router(r.router, dependencies=_ADMIN)


@app.get("/")
def root():
    return {"status": "AZ Elétrica API rodando", "versao": "1.0.0"}
