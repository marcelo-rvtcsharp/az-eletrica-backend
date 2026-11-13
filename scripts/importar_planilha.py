"""
Importa a planilha "Folha_AZ_Eletrica" para o app AZ Elétrica.

O que é importado:
  - Aba "Cadastro": funcionários (nome + diária) e obras (nome + valor do contrato + obs)
  - Aba "Folha Mensal": adicionais e adiantamentos do mês da folha, empréstimos
      parcelados (tabela "DESCONTOS DE ADIANTAMENTOS E MOTO") e salário fixo
      (quem tem valor mensal maior que diárias + extras vira Mensalista)
  - Aba "Alimentação" (se existir): custo de cada dia vira uma despesa de alimentação
  - Aba "Apontamentos": cada linha vira um lançamento no app
      * um apontamento por funcionário por dia
      * cada obra do dia vira um item com o TOTAL do lançamento
        (diárias + hora extra), para o saldo por obra bater com a planilha
      * horas extras ficam anotadas na observação do dia

Pode rodar mais de uma vez: funcionários e obras que já existem (mesmo nome)
não são duplicados, e o apontamento do mesmo funcionário no mesmo dia é
substituído pelo da planilha.

Uso:
    pip install requests openpyxl
    python scripts/importar_planilha.py scripts/dados/Folha_AZ_Eletrica.xlsx
"""

import sys
import getpass
import datetime
import collections
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import openpyxl

BASE_URL = "https://az-eletrica-backend.onrender.com"
PARALELO = 6   # envios simultâneos
TIMEOUT = 120


def norm(s):
    return " ".join(str(s or "").strip().upper().split())


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def ler_planilha(caminho):
    wb = openpyxl.load_workbook(caminho, data_only=True)
    ws = wb["Cadastro"]

    funcionarios = []
    for r in ws.iter_rows(min_row=1, values_only=True):
        if isinstance(r[0], (int, float)) and r[1]:
            funcionarios.append({"nome": str(r[1]).strip(), "valor_diaria": num(r[2])})

    obras = []
    for r in ws.iter_rows(min_row=1, values_only=True):
        if len(r) > 7 and isinstance(r[6], (int, float)) and r[7]:
            obras.append({
                "nome": str(r[7]).strip(),
                "valor": num(r[8]),
                "obs": str(r[10]).strip() if len(r) > 10 and r[10] else None,
            })

    ws = wb["Apontamentos"]
    linhas = []
    for r in ws.iter_rows(min_row=4, values_only=True):
        if not (r[2] and r[3] and isinstance(r[1], datetime.datetime)):
            continue
        linhas.append({
            "data": r[1].date(),
            "func": str(r[2]).strip(),
            "obra": str(r[3]).strip(),
            "diarias": num(r[4]),
            "he": num(r[5]),
            "vl_he": num(r[10]),
            "total": num(r[12]) or (num(r[9]) + num(r[10]) + num(r[11])),
        })
    # Obras marcadas no Cadastro com "JUNTADA COM <OBRA>" na OBS: os lançamentos
    # vão para a outra obra e esta é removida do app.
    juntadas = {}
    for o in obras:
        obs = norm(o["obs"])
        if obs.startswith("JUNTADA COM "):
            destino = obs[len("JUNTADA COM "):].split(" EM ")[0].strip()
            juntadas[norm(o["nome"])] = destino
    obras = [o for o in obras if norm(o["nome"]) not in juntadas]
    nomes = {norm(o["nome"]): o["nome"] for o in obras}
    for l in linhas:
        if norm(l["obra"]) in juntadas:
            l["obra"] = nomes.get(juntadas[norm(l["obra"])], l["obra"])

    folha = ler_folha(wb) or {}
    folha["alimentacao"] = ler_alimentacao(wb)
    return funcionarios, obras, linhas, folha, juntadas


def ler_alimentacao(wb):
    """Aba Alimentação: valor por pessoa (C3) e uma linha por dia (data, ..., PESSOAS, CUSTO)."""
    nome = next((n for n in wb.sheetnames if norm(n).replace("Ç", "C").replace("Ã", "A").startswith("ALIMENT")), None)
    if not nome:
        return []
    ws = wb[nome]
    cab = None
    dias = []
    for r in ws.iter_rows(min_row=1, values_only=True):
        if cab is None:
            if r and norm(r[0]) == "DATA":
                cab = [norm(c) for c in r]
            continue
        if not isinstance(r[0], datetime.datetime):
            continue
        i_p = cab.index("PESSOAS") if "PESSOAS" in cab else None
        i_c = next((i for i, c in enumerate(cab) if c.startswith("CUSTO")), None)
        custo = num(r[i_c]) if i_c is not None else 0
        if custo > 0:
            pessoas = int(num(r[i_p])) if i_p is not None else None
            dias.append({"data": r[0].date(), "valor": round(custo, 2), "pessoas": pessoas})
    return dias


def ler_folha(wb):
    """Lê a aba Folha Mensal: mês/ano, extras por funcionário e empréstimos."""
    if "Folha Mensal" not in wb.sheetnames:
        return None
    ws = wb["Folha Mensal"]
    mes, ano = ws["C2"].value, ws["E2"].value
    if not (isinstance(mes, (int, float)) and isinstance(ano, (int, float))):
        return None
    mes, ano = int(mes), int(ano)

    # Empréstimos: nome na coluna N, depois valor total e valor da parcela
    emprestimos = []
    col = [ws.cell(r, 14).value for r in range(1, ws.max_row + 2)]
    for i, v in enumerate(col[1:], start=1):
        if isinstance(v, str) and v.strip() and i + 2 < len(col) \
                and isinstance(col[i + 1], (int, float)) and isinstance(col[i + 2], (int, float)):
            nome, _, desc = v.partition("-")
            emprestimos.append({"func": nome.strip(), "descricao": (desc.strip().title() or "Empréstimo"),
                                "total": float(col[i + 1]), "parcela": float(col[i + 2])})

    pessoas = []
    for r in ws.iter_rows(min_row=4, values_only=True):
        if not (isinstance(r[0], (int, float)) and r[1]):
            continue
        g, h, i, j, k = (num(r[c]) for c in (6, 7, 8, 9, 10))
        pessoas.append({"func": str(r[1]).strip(), "adicional": i, "adiantamento": k,
                        "fixo": round(j - (g + h + i), 2) if j - (g + h + i) > 0.01 else 0.0})
    return {"ano": ano, "mes": mes, "pessoas": pessoas, "emprestimos": emprestimos}


def main():
    if len(sys.argv) < 2:
        print("Uso: python scripts/importar_planilha.py CAMINHO_DA_PLANILHA.xlsx")
        return
    funcionarios, obras, linhas, folha, juntadas = ler_planilha(sys.argv[1])

    # obras que aparecem nos apontamentos mas não estão no cadastro
    nomes_obras = {norm(o["nome"]) for o in obras}
    for l in linhas:
        if norm(l["obra"]) not in nomes_obras:
            obras.append({"nome": l["obra"], "valor": 0, "obs": "Criada na importação (não estava no cadastro)"})
            nomes_obras.add(norm(l["obra"]))

    grupos = collections.defaultdict(list)
    for l in linhas:
        grupos[(norm(l["func"]), l["data"])].append(l)

    print("=== Importar planilha para o AZ Elétrica ===\n")
    print(f"Funcionários: {len(funcionarios)}")
    print(f"Obras:        {len(obras)}")
    print(f"Lançamentos:  {len(linhas)} linhas -> {len(grupos)} apontamentos (funcionário/dia)")
    print(f"Período:      {min(l['data'] for l in linhas):%d/%m/%Y} a {max(l['data'] for l in linhas):%d/%m/%Y}")
    print(f"Total MO:     R$ {sum(l['total'] for l in linhas):,.2f}")
    if folha and folha.get("alimentacao"):
        al = folha["alimentacao"]
        print(f"Alimentação:  {len(al)} dias, R$ {sum(d['valor'] for d in al):,.2f}")
    if folha and folha.get("pessoas") is not None:
        print(f"Folha {folha['mes']:02d}/{folha['ano']}:   {sum(1 for p in folha['pessoas'] if p['adicional'])} adicionais, "
              f"{sum(1 for p in folha['pessoas'] if p['adiantamento'])} adiantamentos/parcelas, "
              f"{len(folha['emprestimos'])} empréstimos, {sum(1 for p in folha['pessoas'] if p['fixo'])} salário(s) fixo(s)")
    print()

    print("Seu login no app (a primeira conexão pode levar ~1 minuto):")
    email = input("   E-mail: ").strip()
    senha = getpass.getpass("   Senha: ").strip()
    s = requests.Session()
    r = s.post(f"{BASE_URL}/auth/login", data={"username": email, "password": senha}, timeout=TIMEOUT)
    if r.status_code != 200:
        print(f"\nNão foi possível entrar: {r.json().get('detail', r.text)}")
        return
    s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    if input("\nConfirmar importação? (s/n) ").strip().lower() != "s":
        print("Cancelado.")
        return

    # ── Funcionários ──────────────────────────────────────────
    existentes = {norm(e["nome"]): e["id"] for e in s.get(f"{BASE_URL}/eletricistas/", timeout=TIMEOUT).json()}
    novos = 0
    for f in funcionarios:
        if norm(f["nome"]) in existentes:
            continue
        r = s.post(f"{BASE_URL}/eletricistas/", json={"nome": f["nome"], "valor_diaria": f["valor_diaria"]}, timeout=TIMEOUT)
        r.raise_for_status()
        existentes[norm(f["nome"])] = r.json()["id"]
        novos += 1
    ids_func = existentes
    print(f"\nFuncionários: {novos} criados, {len(funcionarios) - novos} já existiam")

    # ── Obras ─────────────────────────────────────────────────
    existentes = {norm(o["nome"]): o["id"] for o in s.get(f"{BASE_URL}/obras/", timeout=TIMEOUT).json()}
    novos = 0
    for o in obras:
        if norm(o["nome"]) in existentes:
            continue
        r = s.post(f"{BASE_URL}/obras/", json={
            "nome": o["nome"], "cliente": o["nome"], "valor_contratado": o["valor"],
            "observacoes": o["obs"],
        }, timeout=TIMEOUT)
        r.raise_for_status()
        existentes[norm(o["nome"])] = r.json()["id"]
        novos += 1
    ids_obra = existentes
    print(f"Obras:        {novos} criadas, {len(obras) - novos} já existiam")

    # ── Apontamentos ──────────────────────────────────────────
    def enviar(chave, itens_planilha):
        func, data = chave
        por_obra = collections.defaultdict(float)
        notas = []
        for l in itens_planilha:
            por_obra[ids_obra[norm(l["obra"])]] += l["total"]
            if l["diarias"] and l["diarias"] != 1:
                notas.append(f"{l['obra']}: {l['diarias']:g} diária(s)")
            if l["he"]:
                notas.append(f"{l['obra']}: {l['he']:g}h extra (R$ {l['vl_he']:.2f})")
        corpo = {
            "eletricista_id": ids_func[func],
            "data": data.isoformat(),
            "observacao": "; ".join(notas) or None,
            "itens": [{"obra_id": oid, "valor": round(v, 2)} for oid, v in por_obra.items()],
            "adicionais": [],
        }
        for tentativa in range(3):
            try:
                r = s.post(f"{BASE_URL}/apontamentos/", json=corpo, timeout=TIMEOUT)
                if r.status_code == 200:
                    return None
                erro = f"{r.status_code} {r.text[:120]}"
            except requests.RequestException as e:
                erro = str(e)
        return f"{func} {data:%d/%m/%Y}: {erro}"

    erros = []
    feitos = 0
    with ThreadPoolExecutor(PARALELO) as ex:
        futuros = [ex.submit(enviar, k, v) for k, v in sorted(grupos.items(), key=lambda kv: kv[0][1])]
        for fut in as_completed(futuros):
            feitos += 1
            e = fut.result()
            if e:
                erros.append(e)
            print(f"\rApontamentos: {feitos}/{len(grupos)}  (erros: {len(erros)})", end="", flush=True)
    print()

    # Remove do app as obras que foram juntadas a outra (se ficaram vazias)
    if juntadas:
        for o in s.get(f"{BASE_URL}/obras/", timeout=TIMEOUT).json():
            if norm(o["nome"]) in juntadas:
                if o.get("total_gasto", 0) == 0:
                    s.delete(f"{BASE_URL}/obras/{o['id']}", timeout=TIMEOUT).raise_for_status()
                    print(f"Obra '{o['nome']}' removida (lançamentos foram para {juntadas[norm(o['nome'])]})")
                else:
                    print(f"! Obra '{o['nome']}' ainda tem lançamentos; não foi removida")

    if folha and folha.get("pessoas") is not None:
        importar_folha(s, folha, ids_func)
    if folha and folha.get("alimentacao"):
        importar_alimentacao(s, folha["alimentacao"])

    if erros:
        print("\nFalharam (rode o script de novo para tentar só repetir tudo com segurança):")
        for e in erros:
            print("  -", e)
    else:
        print("\nImportação concluída sem erros!")


def importar_alimentacao(s, dias):
    """Cria/atualiza a despesa de alimentação de cada dia. Dias editados à mão no app não são tocados."""
    meses = sorted({(d["data"].year, d["data"].month) for d in dias})
    existentes = collections.defaultdict(list)
    for ano, mes in meses:
        r = s.get(f"{BASE_URL}/despesas/?ano={ano}&mes={mes}", timeout=TIMEOUT)
        r.raise_for_status()
        for d in r.json()["despesas"]:
            if d["categoria"] == "alimentacao":
                existentes[d["data"]].append(d)
    criados = iguais = manuais = 0
    for d in dias:
        ja = existentes.get(d["data"].isoformat(), [])
        if any(not x["automatica"] for x in ja):
            manuais += 1
            continue
        if len(ja) == 1 and abs(ja[0]["valor"] - d["valor"]) < 0.01 and ja[0]["pessoas"] == d["pessoas"]:
            iguais += 1
            continue
        for x in ja:
            s.delete(f"{BASE_URL}/despesas/{x['id']}", timeout=TIMEOUT).raise_for_status()
        valor_pessoa = d["valor"] / d["pessoas"] if d["pessoas"] else d["valor"]
        s.post(f"{BASE_URL}/despesas/", json={
            "data": d["data"].isoformat(), "categoria": "alimentacao", "valor": d["valor"],
            "pessoas": d["pessoas"], "automatica": True,
            "descricao": f"{d['pessoas']} pessoa(s) × R$ " + f"{valor_pessoa:.2f}".replace(".", ",") if d["pessoas"] else "Alimentação (planilha)",
        }, timeout=TIMEOUT).raise_for_status()
        criados += 1
    total = sum(d["valor"] for d in dias)
    print(f"\nAlimentação: {len(dias)} dias (R$ {total:,.2f}) -> {criados} lançados, {iguais} já estavam iguais, {manuais} mantidos (editados no app)")


def achar_func(nome, ids_func):
    n = norm(nome)
    if n in ids_func:
        return ids_func[n]
    candidatos = [k for k in ids_func if n in k or k.split(".")[-1].startswith(n.split()[0])]
    return ids_func[candidatos[0]] if len(candidatos) == 1 else None


def importar_folha(s, folha, ids_func):
    import calendar
    ano, mes = folha["ano"], folha["mes"]
    fim_mes = datetime.date(ano, mes, calendar.monthrange(ano, mes)[1]).isoformat()
    print(f"\nFolha de {mes:02d}/{ano}:")

    # Salário fixo -> Mensalista
    elets = {e["id"]: e for e in s.get(f"{BASE_URL}/eletricistas/", timeout=TIMEOUT).json()}
    for p in folha["pessoas"]:
        if not p["fixo"]:
            continue
        eid = achar_func(p["func"], ids_func)
        e = elets.get(eid)
        if not e:
            print(f"  ! funcionário não encontrado para o fixo: {p['func']}")
            continue
        corpo = {k: e.get(k) for k in ["nome", "telefone", "cpf", "funcao", "valor_diaria", "nr10_validade",
                                       "nr35_validade", "data_entrada", "status"]}
        corpo.update(tipo_contrato="Mensalista", salario_fixo=p["fixo"])
        s.put(f"{BASE_URL}/eletricistas/{eid}", json=corpo, timeout=TIMEOUT).raise_for_status()
        print(f"  {e['nome']}: mensalista, fixo R$ {p['fixo']:.2f}")

    # Empréstimos (1ª parcela na folha deste mês)
    existentes = {(x["eletricista_id"], norm(x["descricao"])) for x in s.get(f"{BASE_URL}/financeiro/emprestimos", timeout=TIMEOUT).json()}
    parcela_por_func = collections.defaultdict(float)
    for emp in folha["emprestimos"]:
        eid = achar_func(emp["func"], ids_func)
        if not eid:
            print(f"  ! funcionário não encontrado para o empréstimo: {emp['func']}")
            continue
        parcela_por_func[eid] += emp["parcela"]
        if (eid, norm(emp["descricao"])) in existentes:
            continue
        s.post(f"{BASE_URL}/financeiro/emprestimos", json={
            "eletricista_id": eid, "descricao": emp["descricao"], "valor_total": emp["total"],
            "valor_parcela": emp["parcela"], "inicio": datetime.date(ano, mes, 1).isoformat(),
        }, timeout=TIMEOUT).raise_for_status()
        print(f"  Empréstimo {emp['descricao']} ({emp['func']}): R$ {emp['total']:.2f} em parcelas de R$ {emp['parcela']:.2f}")

    # Adicionais e adiantamentos (a parcela do empréstimo já sai da coluna de adiantamento)
    ja = {(x["eletricista_id"], x["tipo"], round(x["valor"], 2), x["descricao"])
          for x in s.get(f"{BASE_URL}/financeiro/lancamentos?ano={ano}&mes={mes}", timeout=TIMEOUT).json()}
    n = 0
    for p in folha["pessoas"]:
        eid = achar_func(p["func"], ids_func)
        if not eid:
            continue
        itens = [("adicional", p["adicional"], "Adicional (importado da planilha)"),
                 ("adiantamento", round(p["adiantamento"] - parcela_por_func.get(eid, 0), 2), "Adiantamento (importado da planilha)")]
        for tipo, valor, desc in itens:
            if valor <= 0 or (eid, tipo, round(valor, 2), desc) in ja:
                continue
            s.post(f"{BASE_URL}/financeiro/lancamentos", json={
                "eletricista_id": eid, "data": fim_mes, "tipo": tipo, "valor": valor, "descricao": desc,
            }, timeout=TIMEOUT).raise_for_status()
            n += 1
    print(f"  {n} adicionais/adiantamentos lançados")

    f = s.get(f"{BASE_URL}/financeiro/folha?ano={ano}&mes={mes}", timeout=TIMEOUT).json()
    print(f"  Folha {mes:02d}/{ano} no app: bruto R$ {f['totais']['bruto']:,.2f} | a receber R$ {f['totais']['a_receber']:,.2f}")


if __name__ == "__main__":
    main()
