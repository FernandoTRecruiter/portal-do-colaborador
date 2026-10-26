# -*- coding: utf-8 -*-
"""
teste_modo_admin.py: abre o site num Edge de verdade (sem janela) e confere, do começo ao fim:
  1. a página abre sem erro de JavaScript e o que o visitante usa continua funcionando;
  2. o modo admin abre pelo botão Personalizar;
  3. editar um texto na página e clicar em Salvar baixa um index.html com a edição dentro,
     e só o bloco #personalizacao muda (o resto do arquivo fica idêntico);
  4. o arquivo baixado, aberto de novo, mostra a edição para o visitante;
     (no site com aba de dados, como Comunicados ou Preço e datas, o mesmo vale para um dado editado nela)
  5. com o site aberto direto do computador (file://), o Salvar pede o index.html e baixa o novo.

Por que existe: o modo admin grava no próprio arquivo do site; um erro aqui quebra a página
de todo visitante. Rode depois de qualquer mudança no index.html.

Uso (de dentro da pasta do projeto):
    python ferramentas/teste_modo_admin.py
Precisa do Edge ou do Chrome e do pacote websocket-client (pip install websocket-client).
"""
import http.server
import json
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import time

sys.dont_write_bytecode = True  # não deixa __pycache__ na pasta do projeto
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from cdp_util import Edge  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
PROJETO = pathlib.Path(__file__).resolve().parent.parent
ORIGINAL = (PROJETO / "index.html").read_text(encoding="utf-8")
TEXTO = "Texto do teste 4821"
# o que saiu do projeto não pode voltar (a letra entre colchetes evita que este arquivo apareça numa busca por essas palavras)
PROIBIDO = re.compile(r"tok[e]n|api\.git[h]ub|plau[s]ible|clar[i]ty|analyt[i]cs|googletag[m]anager", re.I)
BLOCO = re.compile(r'(<script type="application/json" id="personalizacao" data-site="[^"]*">)([\s\S]*?)(</script>)')

# O que o visitante vê e precisa continuar funcionando: nome -> expressão JavaScript que deve dar verdadeiro.
VISITANTE = {
    "comunicados na tela": "document.querySelectorAll('#lista-avisos > *').length >= 1",
    "calendário com os dias do mês": "document.querySelectorAll('#cal-dias > *').length >= 28",
    "cartões de benefícios": "document.querySelectorAll('#grade-ben > *').length >= 1",
    "busca acha resultado": """(function () {
        var b = document.getElementById('busca');
        // a busca procura no idioma da tela (o padrão do primeiro acesso é inglês)
        b.value = document.documentElement.lang.indexOf('en') === 0 ? 'time off' : 'férias'; b.dispatchEvent(new Event('input'));
        var achou = document.querySelectorAll('#resultados [role=option]').length > 0;
        b.value = ''; b.dispatchEvent(new Event('input')); b.blur();
        return achou;
    })()""",
}

# Dado que muda toda semana, editado na aba própria do painel (None = este site não tem essa aba).
# editar: JavaScript que abre a aba, muda um campo como uma pessoa faria e devolve o valor novo;
# na_pagina: JavaScript que dá verdadeiro se a página mostra o valor novo.
DADOS = {
    "aba": "Comunicados",
    "editar": """(function () {
        Array.prototype.find.call(document.querySelectorAll('#ed-painel [data-aba]'), function (b) { return b.textContent === 'Comunicados'; }).click();
        var campo = document.querySelector('#ed-painel .ed-registro [data-c=titulo]');
        campo.value = 'Comunicado do teste 4821';
        campo.dispatchEvent(new Event('input', { bubbles: true }));
        return campo.value;
    })()""",
    "na_pagina": "document.getElementById('lista-avisos').textContent.indexOf('Comunicado do teste 4821') >= 0",
}

# Acha o primeiro texto editável visível (fora de lista), seleciona tudo e deixa o cursor nele.
EDITAR = """(function () {
  var el = Array.prototype.find.call(document.querySelectorAll('[data-ed-editavel]'), function (e) {
    return e.getBoundingClientRect().width > 0 && !e.closest('[data-edit-lista]');
  });
  if (!el) return null;
  el.scrollIntoView({ block: 'center' });
  el.focus();
  var r = document.createRange(); r.selectNodeContents(el);
  var s = getSelection(); s.removeAllRanges(); s.addRange(r);
  return el.getAttribute('data-edit') || el.getAttribute('data-t');
})()"""
BOTAO_SALVAR = "document.querySelector('#ed-painel [data-ed=salvar]')"

falhas = []


def conferir(nome, ok, detalhe=""):
    print(("OK      " if ok else "FALHOU  ") + nome + ("  -> " + str(detalhe) if detalhe and not ok else ""))
    if not ok:
        falhas.append(nome)


class Servidor(http.server.SimpleHTTPRequestHandler):
    """Serve a pasta do projeto por http; em /_baixado.html devolve o arquivo que o Salvar baixou."""
    baixado = b""

    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(PROJETO), **k)

    def do_GET(self):
        if self.path.startswith("/_baixado.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(Servidor.baixado)
            return
        super().do_GET()

    def log_message(self, *a):
        pass


def pasta_de_downloads(ed):
    # cada rodada baixa numa pasta nova e vazia, para não confundir com um download anterior
    pasta = tempfile.mkdtemp(prefix="teste_admin_")
    try:
        ed.cmd("Browser.setDownloadBehavior", behavior="allow", downloadPath=pasta)
    except RuntimeError:
        ed.cmd("Page.setDownloadBehavior", behavior="allow", downloadPath=pasta)
    return pasta


def esperar_download(ed, pasta, segundos=15):
    alvo = pathlib.Path(pasta) / "index.html"
    fim = time.time() + segundos
    while time.time() < fim:
        if alvo.exists() and alvo.stat().st_size > 0:
            ed.esperar(0.5)
            return alvo.read_text(encoding="utf-8")
        ed.esperar(0.3)
    return None


def editar_texto(ed):
    chave = ed.js(EDITAR)
    if chave:
        ed.cmd("Input.insertText", text=TEXTO)          # digita como uma pessoa
        ed.js("document.activeElement.blur()", espera=0.5)
    return chave


def mostra_edicao(ed, chave):
    # olha o próprio elemento editado: o innerText da página viria em caixa alta onde o CSS manda (text-transform)
    seletor = '[data-edit="%s"],[data-t="%s"]' % (chave, chave)
    return ed.js("Array.prototype.some.call(document.querySelectorAll(%s), function (e) { return e.textContent.indexOf(%s) >= 0; })"
                 % (json.dumps(seletor), json.dumps(TEXTO)))


def clicar_salvar(ed):
    # clique de mouse de verdade (o navegador só abre a escolha de arquivo depois de um gesto da pessoa)
    x, y = ed.js("(function(){var r=%s.getBoundingClientRect();return [r.left+r.width/2, r.top+r.height/2];})()" % BOTAO_SALVAR)
    ed.clicar(x, y)


def main():
    servidor = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Servidor)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    endereco = "http://127.0.0.1:%d/" % servidor.server_address[1]

    achado = PROIBIDO.search(ORIGINAL)
    conferir("index.html sem resto do que saiu (publicação pelo GitHub, métricas)", not achado, achado and achado.group(0))

    with Edge(1440, 900) as ed:
        # 1. visitante
        ed.ir(endereco + "index.html", espera=3)
        conferir("página abre sem erro de JavaScript", not ed.erros(), "; ".join(ed.erros()[:2]))
        for nome, expressao in VISITANTE.items():
            conferir("visitante: " + nome, bool(ed.js(expressao)))

        # 2. modo admin pelo botão do rodapé
        ed.js("document.querySelector('[data-abre-admin]').click()", espera=0.5)
        painel = ed.js("(function(){var p=document.getElementById('ed-painel');return p&&!p.hidden?p.textContent:'';})()")
        conferir("modo admin abre pelo botão Personalizar", bool(painel))
        conferir("painel sem nada da publicação pelo GitHub", not re.search(r"tok[e]n|git[h]ub", painel or "", re.I))

        # 3. editar e salvar (site aberto por http, como no GitHub Pages)
        pasta = pasta_de_downloads(ed)
        chave = editar_texto(ed)
        conferir("há texto editável na página", bool(chave))
        conferir("a edição aparece na página", mostra_edicao(ed, chave))
        if DADOS:
            valor = ed.js(DADOS["editar"])
            conferir("aba %s: a edição aparece na página" % DADOS["aba"], bool(valor) and bool(ed.js(DADOS["na_pagina"])))
        conferir("botão Salvar avisa que há mudança", ed.js(BOTAO_SALVAR + ".textContent.indexOf('●') >= 0"))
        clicar_salvar(ed)
        baixado = esperar_download(ed, pasta)
        conferir("Salvar baixa o index.html", baixado is not None)
        if baixado:
            bloco = BLOCO.search(baixado)
            textos = json.loads(bloco.group(2)).get("textos", {}) if bloco else {}
            conferir("a edição está no bloco #personalizacao", TEXTO in textos.values())
            if DADOS:
                dados = json.loads(bloco.group(2)).get("dados", {}) if bloco else {}
                conferir("aba %s: o valor novo está no arquivo baixado" % DADOS["aba"], str(valor) in json.dumps(dados, ensure_ascii=False))
            conferir("fora o bloco, o arquivo baixado é idêntico ao original", BLOCO.sub(r"\1{}\3", baixado) == ORIGINAL)
            achado = PROIBIDO.search(baixado)
            conferir("arquivo baixado sem resto do que saiu (publicação pelo GitHub, métricas)", not achado, achado and achado.group(0))
            conferir("botão Salvar volta ao normal depois de salvar", ed.js(BOTAO_SALVAR + ".textContent === 'Salvar'"))

            # 4. o visitante abre o arquivo baixado
            Servidor.baixado = baixado.encode("utf-8")
            ed.eventos.clear()
            ed.ir(endereco + "_baixado.html", espera=3)
            conferir("arquivo baixado abre sem erro de JavaScript", not ed.erros(), "; ".join(ed.erros()[:2]))
            conferir("visitante vê a edição no arquivo baixado", mostra_edicao(ed, chave))
            if DADOS:
                conferir("aba %s: visitante vê o valor novo no arquivo baixado" % DADOS["aba"], bool(ed.js(DADOS["na_pagina"])))
        shutil.rmtree(pasta, ignore_errors=True)

        # 5. site aberto direto do computador (file://)
        pasta = pasta_de_downloads(ed)
        ed.eventos.clear()
        ed.ir((PROJETO / "index.html").as_uri() + "#admin", espera=3)
        conferir("file://: abre sem erro e #admin já abre o modo admin",
                 not ed.erros() and ed.js("!document.getElementById('ed-painel').hidden"), "; ".join(ed.erros()[:2]))
        ed.cmd("Page.setInterceptFileChooserDialog", enabled=True)
        editar_texto(ed)
        clicar_salvar(ed)
        ed.esperar(1)
        pedido = next((e for e in ed.eventos if e.get("method") == "Page.fileChooserOpened"), None)
        conferir("file://: Salvar pede para escolher o index.html", pedido is not None)
        if pedido:
            ed.cmd("DOM.setFileInputFiles", files=[str(PROJETO / "index.html")], backendNodeId=pedido["params"]["backendNodeId"])
            baixado = esperar_download(ed, pasta)
            conferir("file://: baixa o index.html com a edição", bool(baixado) and TEXTO in baixado)
        shutil.rmtree(pasta, ignore_errors=True)

    servidor.shutdown()
    print()
    print("Tudo certo." if not falhas else "%d falha(s): %s" % (len(falhas), "; ".join(falhas)))
    sys.exit(1 if falhas else 0)


if __name__ == "__main__":
    main()
