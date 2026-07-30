# AZ Elétrica — Backend API

FastAPI + SQLite · Deploy Railway

## Endpoints

| Método | Rota | Descrição |
|--------|------|-----------|
| POST | /auth/registro | Criar conta |
| POST | /auth/login | Login (retorna token JWT) |
| GET | /auth/me | Usuário logado |
| GET | /dashboard | Resumo geral |
| GET/POST | /eletricistas | Listar / criar |
| GET/PUT/DELETE | /eletricistas/{id} | Detalhe / editar / excluir |
| GET/POST | /obras | Listar / criar |
| GET/PUT/DELETE | /obras/{id} | Detalhe / editar / excluir |
| GET/POST | /apontamentos | Listar / criar |
| DELETE | /apontamentos/{id} | Excluir |
| GET/POST | /orcamentos | Listar / criar |
| GET/PUT/DELETE | /orcamentos/{id} | Detalhe / editar / excluir |

## Rodar localmente

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Acesse: http://localhost:8000/docs

## Deploy Railway

1. Crie conta em railway.app
2. New Project → Deploy from GitHub
3. Selecione este repositório
4. Adicione variável de ambiente: SECRET_KEY=sua-chave-secreta
5. Railway detecta o Procfile automaticamente
