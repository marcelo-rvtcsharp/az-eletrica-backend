from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta
from app.models import Obra, Eletricista, Apontamento, ApontamentoItem, Orcamento, get_db
from app.auth import get_usuario_atual

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/")
def resumo(db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    hoje = datetime.utcnow().date()
    inicio_semana = hoje - timedelta(days=hoje.weekday())
    fim_semana = inicio_semana + timedelta(days=6)
    inicio_mes = hoje.replace(day=1)

    # Total diárias semana
    total_semana = db.query(func.sum(ApontamentoItem.valor)).join(Apontamento).filter(
        Apontamento.data >= inicio_semana,
        Apontamento.data <= fim_semana
    ).scalar() or 0

    # Total diárias mês
    total_mes = db.query(func.sum(ApontamentoItem.valor)).join(Apontamento).filter(
        Apontamento.data >= inicio_mes
    ).scalar() or 0

    # Obras
    obras = db.query(Obra).all()
    obras_resumo = []
    total_contratado = 0
    total_gasto = 0
    obras_negativas = 0
    obras_atencao = 0

    for o in obras:
        if o.status == "Concluída":
            continue
        gasto = db.query(func.sum(ApontamentoItem.valor)).filter(
            ApontamentoItem.obra_id == o.id
        ).scalar() or 0
        saldo = o.valor_contratado - gasto
        perc = round(gasto / o.valor_contratado * 100, 1) if o.valor_contratado > 0 else 0
        situacao = "negativo" if saldo < 0 else "atencao" if perc >= o.alerta_percentual else "ok"
        if situacao == "negativo": obras_negativas += 1
        if situacao == "atencao": obras_atencao += 1
        total_contratado += o.valor_contratado
        total_gasto += gasto
        obras_resumo.append({
            "id": o.id,
            "nome": o.nome,
            "cliente": o.cliente,
            "cidade": o.cidade,
            "valor_contratado": o.valor_contratado,
            "total_gasto": round(gasto, 2),
            "saldo": round(saldo, 2),
            "percentual": perc,
            "situacao": situacao,
        })

    # Eletricistas ativos
    eletricistas_ativos = db.query(Eletricista).filter(Eletricista.status == "Ativo").count()

    # Últimos apontamentos
    ultimos = db.query(Apontamento).order_by(Apontamento.data.desc(), Apontamento.criado_em.desc()).limit(10).all()
    ultimos_lista = []
    for a in ultimos:
        elet = db.query(Eletricista).filter(Eletricista.id == a.eletricista_id).first()
        itens = db.query(ApontamentoItem).filter(ApontamentoItem.apontamento_id == a.id).all()
        for item in itens:
            obra = db.query(Obra).filter(Obra.id == item.obra_id).first()
            ultimos_lista.append({
                "eletricista": elet.nome if elet else "",
                "obra": obra.nome if obra else "",
                "valor": item.valor,
                "data": str(a.data),
            })

    return {
        "semana": {
            "inicio": str(inicio_semana),
            "fim": str(fim_semana),
            "total_diarias": round(total_semana, 2),
        },
        "mes": {
            "total_diarias": round(total_mes, 2),
        },
        "obras": {
            "ativas": len(obras_resumo),
            "negativas": obras_negativas,
            "atencao": obras_atencao,
            "total_contratado": round(total_contratado, 2),
            "total_gasto": round(total_gasto, 2),
            "saldo_geral": round(total_contratado - total_gasto, 2),
            "lista": sorted(obras_resumo, key=lambda x: x["percentual"], reverse=True),
        },
        "equipe": {
            "ativos": eletricistas_ativos,
        },
        "ultimos_apontamentos": ultimos_lista[:8],
    }
