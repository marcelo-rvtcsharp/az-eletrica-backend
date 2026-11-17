"""
Área do diarista (perfil "diarista"):
- apontar a própria diária (hoje e até 2 dias atrás);
- pedir uma obra nova quando o local não está na lista;
- ver e marcar onde estão as ferramentas (com foto);
- ver o próprio mês.
O diarista não vê obras/contratos, custos, outros funcionários, folha nem orçamentos.
"""
import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.auth import exigir_diarista
from app.models import Apontamento, ApontamentoItem, Eletricista, EquipeMembro, Obra, Usuario, get_db
from app.routes import ferramentas as ferr
from app.routes.notificacoes import notificar_admins

router = APIRouter(prefix="/portal", tags=["portal do diarista"])
FORTALEZA = timezone(timedelta(hours=-3))
DIAS_PARA_TRAS = 2
RE_HE = re.compile(r"(\d+(?:[.,]\d+)?)\s*h\s*extra", re.I)
DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def _hoje() -> date:
    return datetime.now(FORTALEZA).date()


def _eletricista(db: Session, u: Usuario) -> Eletricista:
    e = db.get(Eletricista, u.eletricista_id)
    if not e:
        raise HTTPException(404, "Seu cadastro de funcionário não foi encontrado. Fale com o escritório.")
    if (e.status or "Ativo") != "Ativo":
        raise HTTPException(403, "Seu cadastro está inativo. Fale com o escritório.")
    return e


def _equipe(db: Session, u: Usuario) -> list:
    """Funcionários que este encarregado pode apontar (além dele mesmo)."""
    ids = [m.eletricista_id for m in db.query(EquipeMembro).filter(EquipeMembro.usuario_id == u.id).all()]
    if not ids:
        return []
    return (db.query(Eletricista).filter(Eletricista.id.in_(ids), Eletricista.status == "Ativo")
            .order_by(Eletricista.nome).all())


def _usuario_do(db: Session, eletricista_id: int):
    x = db.query(Usuario.id).filter(Usuario.eletricista_id == eletricista_id, Usuario.perfil == "diarista").first()
    return x[0] if x else None


def _obras_abertas(db: Session):
    return (db.query(Obra).filter(Obra.status != "Concluída").order_by(Obra.nome).all())


def _ser_apont(a: Apontamento, u: Usuario, e: Eletricista, pode_editar=None) -> dict:
    he_item = sum(float(x.replace(",", ".")) for x in RE_HE.findall(a.observacao or ""))  # HE somada no valor da obra
    d = float(e.valor_diaria or 0)
    he_adic = 0.0                                                                       # HE lançada como adicional
    for ad in (a.adicionais or []):
        if ad.tipo == "hora_extra":
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*h", ad.descricao or "")
            he_adic += float(m.group(1).replace(",", ".")) if m else (ad.valor / (d / 8) if d else 0)
    he = round(he_item + he_adic, 2)
    itens = sorted(a.itens, key=lambda i: i.id or 0)

    def fracao(k, i):
        v = i.valor - (d / 8 * he_item if k == 0 else 0)
        return 0.5 if d and v < d * 0.75 else 1
    return {
        "data": a.data.isoformat(),
        "obras": [{"obra_id": i.obra_id, "obra_nome": i.obra.nome if i.obra else "?", "fracao": fracao(k, i)}
                  for k, i in enumerate(itens)],
        "horas_extras": he,
        "observacao": a.observacao,
        "meu": a.criado_por == u.id,
        "origem": "voce" if a.criado_por == u.id else
                  ("encarregado" if "pelo encarregado" in (a.observacao or "") else
                   ("funcionario" if "pelo funcionário no app" in (a.observacao or "") else "escritorio")),
        "editavel": (a.criado_por in (pode_editar or {u.id})) and a.data >= _hoje() - timedelta(days=DIAS_PARA_TRAS),
    }


@router.get("/inicio")
def inicio(ano: Optional[int] = None, mes: Optional[int] = None,
           db: Session = Depends(get_db), u: Usuario = Depends(exigir_diarista)):
    e = _eletricista(db, u)
    hoje = _hoje()
    ano, mes = ano or hoje.year, mes or hoje.month
    ini = date(ano, mes, 1)
    fim = date(ano + (mes == 12), mes % 12 + 1, 1)
    aps = (db.query(Apontamento).options(joinedload(Apontamento.itens).joinedload(ApontamentoItem.obra), joinedload(Apontamento.adicionais))
           .filter(Apontamento.eletricista_id == e.id, Apontamento.data >= ini, Apontamento.data < fim)
           .order_by(Apontamento.data).all())
    lista = [_ser_apont(a, u, e) for a in aps]
    # dias recentes (para saber se já apontou hoje/ontem, mesmo na virada do mês)
    recentes = (db.query(Apontamento).options(joinedload(Apontamento.itens).joinedload(ApontamentoItem.obra), joinedload(Apontamento.adicionais))
                .filter(Apontamento.eletricista_id == e.id,
                        Apontamento.data >= hoje - timedelta(days=DIAS_PARA_TRAS), Apontamento.data <= hoje).all())
    ultimo = (db.query(ApontamentoItem.obra_id).join(Apontamento, Apontamento.id == ApontamentoItem.apontamento_id)
              .filter(Apontamento.eletricista_id == e.id).order_by(Apontamento.data.desc()).first())
    obras = _obras_abertas(db)
    equipe = []
    for m in _equipe(db, u):
        permitidos = {u.id, _usuario_do(db, m.id)}
        rec = (db.query(Apontamento).options(joinedload(Apontamento.itens).joinedload(ApontamentoItem.obra), joinedload(Apontamento.adicionais))
               .filter(Apontamento.eletricista_id == m.id,
                       Apontamento.data >= hoje - timedelta(days=DIAS_PARA_TRAS), Apontamento.data <= hoje).all())
        equipe.append({"id": m.id, "nome": m.nome,
                       "recentes": {a.data.isoformat(): _ser_apont(a, u, m, permitidos) for a in rec}})
    return {
        "funcionario": {"id": e.id, "nome": e.nome, "funcao": e.funcao},
        "equipe": equipe,
        "usuario": {"nome": u.nome, "email": u.email, "senha_padrao": bool(u.senha_padrao)},
        "hoje": hoje.isoformat(),
        "dias_para_tras": DIAS_PARA_TRAS,
        "ano": ano, "mes": mes,
        "apontamentos": lista,
        "recentes": {a.data.isoformat(): _ser_apont(a, u, e) for a in recentes},
        "resumo": {"dias": len({a["data"] for a in lista}),
                   "horas_extras": sum(a["horas_extras"] for a in lista),
                   "obras": len({o["obra_id"] for a in lista for o in a["obras"]})},
        "ultima_obra_id": ultimo[0] if ultimo else None,
        "obras": [{"id": o.id, "nome": o.nome, "cidade": o.cidade, "solicitada": o.status == "Solicitada"} for o in obras],
    }


class ObraDia(BaseModel):
    obra_id: int
    fracao: float = 1


class ApontarSchema(BaseModel):
    data: date
    obras: List[ObraDia]
    horas_extras: float = 0
    observacao: Optional[str] = None
    eletricista_ids: Optional[List[int]] = None   # encarregado: ele e/ou a equipe; vazio = só ele


def _fmt_h(h: float) -> str:
    return (f"{h:.1f}".rstrip("0").rstrip(".")).replace(".", ",")


@router.post("/apontar")
def apontar(data: ApontarSchema, db: Session = Depends(get_db), u: Usuario = Depends(exigir_diarista)):
    e = _eletricista(db, u)
    hoje = _hoje()
    if data.data > hoje:
        raise HTTPException(400, "Não dá para apontar um dia que ainda não chegou")
    if data.data < hoje - timedelta(days=DIAS_PARA_TRAS):
        raise HTTPException(400, f"Só é possível apontar até {DIAS_PARA_TRAS} dias atrás. Fale com o escritório.")
    if not data.obras or len(data.obras) > 2:
        raise HTTPException(400, "Escolha 1 ou 2 obras")
    if len({o.obra_id for o in data.obras}) != len(data.obras):
        raise HTTPException(400, "Escolha obras diferentes")
    if any(o.fracao not in (0.5, 1) for o in data.obras):
        raise HTTPException(400, "Use dia inteiro ou meio dia")
    total = sum(o.fracao for o in data.obras)
    if total > 1:
        raise HTTPException(400, "A soma das obras não pode passar de 1 diária")
    he = float(data.horas_extras or 0)
    if he < 0 or he > 8 or (he * 2) % 1:
        raise HTTPException(400, "Horas extras de 0 a 8, de meia em meia hora")
    obras = {o.id: o for o in _obras_abertas(db)}
    for o in data.obras:
        if o.obra_id not in obras:
            raise HTTPException(400, "Obra não encontrada ou já concluída")

    # para quem: ele mesmo e/ou membros da equipe dele
    equipe = {m.id: m for m in _equipe(db, u)}
    ids = list(dict.fromkeys(data.eletricista_ids or [e.id]))
    pessoas = []
    for i in ids:
        if i == e.id:
            pessoas.append((e, {u.id}))
        elif i in equipe:
            pessoas.append((equipe[i], {u.id, _usuario_do(db, i)}))
        else:
            raise HTTPException(403, "Você só pode apontar a sua diária e a da sua equipe")
    # confere tudo antes de gravar
    existentes = {}
    for pessoa, permitidos in pessoas:
        ex = db.query(Apontamento).filter(Apontamento.eletricista_id == pessoa.id, Apontamento.data == data.data).first()
        if ex and ex.criado_por not in permitidos:
            quem = "Seu dia" if pessoa.id == e.id else f"O dia de {pessoa.nome}"
            raise HTTPException(409, f"{quem} já foi lançado pelo escritório. Para corrigir, fale com o engenheiro.")
        existentes[pessoa.id] = ex

    obs = (data.observacao or "").strip()
    resultado, corrigiu = [], False
    for pessoa, permitidos in pessoas:
        ex = existentes[pessoa.id]
        if ex:
            corrigiu = True
            db.delete(ex)
            db.flush()
        diaria = float(pessoa.valor_diaria or 0)
        v_he = round(diaria / 8 * he, 2)
        partes = []
        if len(data.obras) > 1 or total < 1:
            partes.append("; ".join(f"{obras[o.obra_id].nome}: {o.fracao:g} diária(s)".replace(".", ",") for o in data.obras))
        if he:
            partes.append(f"{obras[data.obras[0].obra_id].nome}: {_fmt_h(he)}h extra (R$ {v_he:.2f})")
        if obs:
            partes.append("Obs.: " + obs[:300])
        partes.append("Apontado pelo funcionário no app" if pessoa.id == e.id else f"Apontado pelo encarregado {e.nome} no app")
        apto = Apontamento(eletricista_id=pessoa.id, data=data.data, observacao=" · ".join(partes), criado_por=u.id)
        db.add(apto)
        db.flush()
        for k, o in enumerate(data.obras):
            db.add(ApontamentoItem(apontamento_id=apto.id, eletricista_id=pessoa.id, obra_id=o.obra_id,
                                   valor=round(diaria * o.fracao + (v_he if k == 0 else 0), 2)))
        resultado.append((apto.id, pessoa, permitidos))
    db.commit()

    dia_txt = f"{DIAS_SEMANA[data.data.weekday()]} {data.data.strftime('%d/%m')}"
    locais = " + ".join(obras[o.obra_id].nome + (" (½)" if o.fracao == 0.5 else "") for o in data.obras)
    nomes = [p.nome for p, _ in pessoas]
    if len(pessoas) == 1 and pessoas[0][0].id == e.id:
        titulo = f"{'Diária corrigida' if corrigiu else 'Diária apontada'} · {e.nome}"
        texto = f"{dia_txt} · {locais}"
    else:
        titulo = f"{'Diárias corrigidas' if corrigiu else 'Diárias apontadas'} por {e.nome} · {len(pessoas)} pessoa(s)"
        texto = f"{dia_txt} · {locais} · {', '.join(nomes)}"
    notificar_admins(db, titulo, texto + (f" · {_fmt_h(he)}h extra" if he else "") + (f" · {obs[:80]}" if obs else ""),
                     tipo="diaria", link="apontamentos")
    saida = []
    for aid, pessoa, permitidos in resultado:
        a = (db.query(Apontamento).options(joinedload(Apontamento.itens).joinedload(ApontamentoItem.obra), joinedload(Apontamento.adicionais))
             .filter(Apontamento.id == aid).first())
        d = _ser_apont(a, u, pessoa, permitidos)
        d["eletricista_id"], d["nome"] = pessoa.id, pessoa.nome
        saida.append(d)
    return {"itens": saida, "corrigiu": corrigiu}


@router.delete("/apontar/{dia}")
def desfazer(dia: date, eletricista_id: Optional[int] = None, db: Session = Depends(get_db),
             u: Usuario = Depends(exigir_diarista)):
    e = _eletricista(db, u)
    permitidos = {u.id}
    if eletricista_id and eletricista_id != e.id:
        membro = {m.id: m for m in _equipe(db, u)}.get(eletricista_id)
        if not membro:
            raise HTTPException(403, "Esse funcionário não é da sua equipe")
        permitidos.add(_usuario_do(db, membro.id))
        e = membro
    a = db.query(Apontamento).filter(Apontamento.eletricista_id == e.id, Apontamento.data == dia).first()
    if not a:
        raise HTTPException(404, "Nada apontado nesse dia")
    if a.criado_por not in permitidos or dia < _hoje() - timedelta(days=DIAS_PARA_TRAS):
        raise HTTPException(403, "Esse dia não pode mais ser apagado por aqui. Fale com o escritório.")
    db.delete(a)
    db.commit()
    notificar_admins(db, f"Diária apagada · {e.nome}", f"{dia.strftime('%d/%m')} foi removida pelo funcionário",
                     tipo="diaria", link="apontamentos")
    return {"ok": True}


class SolicitarObraSchema(BaseModel):
    nome: str
    cidade: Optional[str] = None
    observacao: Optional[str] = None


def _chave(t: str) -> str:
    import unicodedata
    t = "".join(c for c in unicodedata.normalize("NFD", t or "") if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", t.lower())


@router.post("/obras/solicitar")
def solicitar_obra(data: SolicitarObraSchema, db: Session = Depends(get_db), u: Usuario = Depends(exigir_diarista)):
    e = _eletricista(db, u)
    nome = re.sub(r"\s+", " ", (data.nome or "").strip()).upper()
    if len(nome) < 3:
        raise HTTPException(400, "Escreva o nome do local (pelo menos 3 letras)")
    for o in db.query(Obra).filter(Obra.status != "Concluída").all():
        if _chave(o.nome) == _chave(nome):
            return {"id": o.id, "nome": o.nome, "cidade": o.cidade, "solicitada": o.status == "Solicitada",
                    "ja_existia": True}
    hoje = _hoje()
    obs = f"Pedida por {e.nome} pelo app em {hoje.strftime('%d/%m/%Y')}."
    if data.observacao and data.observacao.strip():
        obs += " " + data.observacao.strip()[:300]
    o = Obra(nome=nome[:120], cliente="A definir", cidade=(data.cidade or "").strip()[:80] or None,
             valor_contratado=0, status="Solicitada", observacoes=obs, data_inicio=hoje)
    db.add(o)
    db.commit()
    db.refresh(o)
    notificar_admins(db, f"Nova obra pedida · {e.nome}",
                     f"{o.nome}{' · ' + o.cidade if o.cidade else ''}. Confira em Obras.", tipo="obra", link="obras")
    return {"id": o.id, "nome": o.nome, "cidade": o.cidade, "solicitada": True, "ja_existia": False}


# ── Ferramentas ─────────────────────────────────────────────────────────────
@router.get("/ferramentas")
def ferramentas(db: Session = Depends(get_db), u: Usuario = Depends(exigir_diarista)):
    e = _eletricista(db, u)
    comigo = f"com {e.nome}".lower()
    lista = ferr.listar(db=db, _=u)
    for f in lista:
        v = f.get("ultimo_visto")
        f["comigo"] = bool(v and v["local"].strip().lower() == comigo)
    return {"local_comigo": f"Com {e.nome}", "ferramentas": lista}


@router.post("/ferramentas/{id}/visto")
async def marcar_ferramenta(
    id: int, local: Optional[str] = Form(None), obra_id: Optional[int] = Form(None),
    observacao: Optional[str] = Form(None), foto: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db), u: Usuario = Depends(exigir_diarista),
):
    return await ferr.registrar_visto(id=id, local=local, obra_id=obra_id, data=_hoje(), observacao=observacao,
                                      foto=foto, db=db, usuario=u)


@router.get("/ferramentas/vistos/{vid}/foto")
def foto(vid: int, db: Session = Depends(get_db), u: Usuario = Depends(exigir_diarista)):
    return ferr.foto_visto(vid=vid, db=db, _=u)
