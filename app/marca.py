"""Identidade visual da AZ Elétrica usada nos PDFs (cores + caminhos do logo)."""
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"
LOGO_MARCA = str(STATIC / "logo-mark.png")      # só o "AZ" com o raio
LOGO_COMPLETO = str(STATIC / "logo-full.png")   # AZ + slogan + ELÉTRICA (para fundo azul)
LOGO_MARCA_PROP = 520 / 228                     # largura / altura

AZUL = "#0E2A9E"
AZUL_CLARO = "#1D3FD0"
AMARELO = "#FFD21F"
SLOGAN = "Sua energia em boas mãos!"

EMPRESA = "AZ ELÉTRICA LTDA"
CNPJ = "63.824.949/0001-26"
CONTATO = "Fone (88) 98161-0318 · Elderazevedo22@hotmail.com"
