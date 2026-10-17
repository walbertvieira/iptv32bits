# IPTV Player 3.2 — edição 32 bits, com VLC embutido

Mesmo programa da v3.2 (playlists salvas, Continuar assistindo, downloads offline e tela sempre ligada), preparado para **Windows 10 de 32 bits** e **sem precisar instalar o VLC**: o motor do VLC vai dentro da pasta do programa (subpasta `vlc`).

Funciona também em Windows 10/11 de 64 bits.

## O que mudou em relação à v3.2 (64 bits)
- **Interface em Qt 5.15 (PyQt5)**: o Qt 6/PySide6 usado na v3.2 não existe para Windows 32 bits. Visual e funções iguais.
- **VLC 3.0.24 portátil embutido**: nada para instalar. Se a pasta `vlc` estiver junto do `.exe`, é ela que o programa usa.
- **Capas https**: se o Qt 32 bits não tiver OpenSSL, as capas `https://` são baixadas pelo Python, sem perder nenhuma imagem.
- **Uso de memória controlado**: o cache de capas na memória tem limite. Isso importa num programa de 32 bits, que tem cerca de 2 GB disponíveis.
- **Registro de erros**: erros inesperados são gravados em `%APPDATA%\IPTVPlayer\erro.log` em vez de fechar o programa.
- **Autoteste** (`IPTV Player.exe --selftest arquivo.log`): verifica interface, catálogo, séries, favoritos, histórico, downloads e o VLC embutido. Os scripts de build rodam o autoteste sozinhos.
- Playlists, histórico, favoritos e downloads ficam no mesmo lugar da v3.2 (`%APPDATA%\IPTVPlayer`). Quem já usava a v3.2 no mesmo PC continua com tudo.

## Como obter o programa pronto (pasta portátil)

### Opção A — GitHub (sem instalar nada no seu PC)
1. Envie o conteúdo desta pasta para um repositório no GitHub, como foi feito com o app Android. Confira se a pasta `.github` subiu junto.
2. Abra a aba **Actions** › **"Gerar IPTV Player (Windows 32 e 64 bits)"** › **Run workflow**.
3. Em cerca de 10 minutos, baixe o artefato **IPTV-Player-32bits**. Também sai um 64 bits, se precisar.
4. Extraia o zip e abra **`IPTV Player\IPTV Player.exe`**.

O build só entrega o pacote se o autoteste do `.exe` passar. Os logs ficam no artefato `autoteste-32bits`.

### Opção B — no seu PC
1. Instale o **Python 3.12, "Windows installer (32-bit)"**, de https://www.python.org/downloads/windows/. Deixe marcadas as opções *py launcher* e *Add python.exe to PATH*. Pode ser instalado em um PC 32 ou 64 bits.
2. Dê dois cliques em **`gerar_exe.bat`**. Ele instala as dependências, baixa o VLC 32 bits portátil, gera o executável, copia o VLC para dentro e roda o autoteste.
3. O resultado fica em **`dist\IPTV Player\`**. Copie essa **pasta inteira** para o PC 32 bits e abra o `IPTV Player.exe`.

O PC de destino não precisa de Python nem de VLC. Para rodar pelo código-fonte sem gerar o `.exe`, use o `executar.bat`.

## Estrutura da pasta pronta
```
IPTV Player\
├── IPTV Player.exe
├── _internal\        (Python, Qt e bibliotecas)
├── vlc\              (VLC 3.0.24 portátil: libvlc.dll, libvlccore.dll, plugins\)
├── LEIA-ME.md
└── exemplo.m3u
```
Não separe o `.exe` das pastas `_internal` e `vlc`.

## Novidades da versão 3.2
- **Tela sempre ligada durante a reprodução**: enquanto um vídeo está tocando (ou carregando), o Windows não apaga a tela, não liga o protetor de tela e não entra em suspensão. Ao pausar, parar, terminar o vídeo ou fechar o programa, a economia de energia volta ao normal.

## Versão 3 — downloads offline
- **Baixar filmes e episódios**: botão direito › *Baixar para assistir offline*, ou o botão ⬇ no player. Nas séries: *Baixar série inteira* ou *Baixar temporada*.
- **Sempre usa o arquivo baixado**: se o conteúdo já foi baixado, o player abre o arquivo do disco **mesmo com internet** (selo verde "⬇ OFFLINE" no player). Continuar assistindo e o histórico funcionam normalmente.
- **Aba Baixados**: tudo que foi baixado ou está baixando, com filtros *Baixando / na fila*, *Filmes* e *Séries*; progresso, velocidade e tempo restante ao vivo; espaço total ocupado.
- **Downloads robustos**: pausar/retomar sem perder o que já baixou; se a internet cair, tenta de novo sozinho e continua do ponto em que parou; ao fechar o programa, os downloads retomam na próxima abertura. Verifica espaço livre em disco antes de começar.
- **Organização em pastas**: `Vídeos\IPTV Player\Filmes\` e `Vídeos\IPTV Player\<nome da série>\`. A pasta pode ser alterada em *Opções de download* (aba Baixados), junto com o número de downloads simultâneos (padrão 2), pausar/retomar todos e abrir a pasta.
- No catálogo, capas com selo ⬇ azul = baixado (laranja = baixando). Nas séries aparece "X baixado(s)".
- Botão direito em um item baixado: *Mostrar arquivo na pasta* ou *Excluir arquivo baixado*.
- Arquivos diretos (MP4, MKV, AVI… — o normal em filmes e séries de listas IPTV) são baixados em velocidade máxima. Streams HLS (.m3u8) são gravados pelo VLC em .ts e podem levar mais tempo; esse tipo não tem retomada.
- Canais ao vivo não podem ser baixados.

## Versão 2
- **Minhas playlists**: cada playlist é copiada para dentro do aplicativo. Ao abrir o programa, a última playlist e o catálogo aparecem na hora, direto do cache local (funciona até offline).
- **Atualização em segundo plano**: listas por URL são baixadas de novo a cada 12 h (configurável) sem travar a tela. Se o servidor falhar, a cópia salva continua em uso. Arquivos locais são relidos quando o arquivo muda.
- **Continuar assistindo**: aba com o que você começou a ver, com % assistido e tempo restante. Um clique retoma exatamente de onde parou. Ao terminar um episódio, o próximo entra na lista (e começa automaticamente).
- **Histórico no catálogo**: capas com barra vermelha de progresso e selo verde ✓ nos já assistidos. Nas séries aparece "X visto(s)".
- **Controles de mídia redesenhados**: botões redondos com ícones brancos, botão Play grande em azul, botões de ±10 s, selo vermelho "AO VIVO", barra de progresso vermelha e volume em %.
- A janela reabre do mesmo tamanho e na mesma aba/grupo em que você fechou.

## Atalhos
| Tecla | Ação |
|---|---|
| Duplo clique / Enter | Reproduzir / abrir série |
| Espaço | Play / pausa |
| F11 ou F / Esc | Tela cheia / sair |
| Ctrl+← / Ctrl+→ | Voltar / avançar 10 s (Shift: 60 s) |
| Ctrl+↑ / Ctrl+↓ | Volume |
| M | Mudo |
| Ctrl+PgUp / Ctrl+PgDown | Item anterior / próximo |
| Ctrl+D | Favoritar o que está tocando |
| Alt+← | Voltar da série para o catálogo |
| Ctrl+N | Adicionar playlist |
| F5 | Atualizar a playlist |

Botão direito em um item: assistir do início, marcar como assistido/não assistido, remover de Continuar assistindo, favoritar, copiar URL.

## Dados salvos
`%APPDATA%\IPTVPlayer\`, compartilhado com a v3.2:
- `config.json`: playlists, favoritos, histórico, preferências e estado da janela.
- `playlists\`: cópia de cada playlist e o catálogo pronto.
- `cache_logos\` e `cache_logos_https\`: capas e logos.
- `erro.log`: registro de erros, se houver.

Vídeos baixados ficam em `Vídeos\IPTV Player\`, ou na pasta escolhida em *Opções de download*.

## Problemas comuns
- **"O VLC não foi encontrado"**: a pasta `vlc` não está ao lado do `IPTV Player.exe`. Extraia o pacote completo de novo.
- **Vídeo 4K/HEVC travando num PC 32 bits antigo**: o limite é o processador ou a placa de vídeo. Prefira a versão HD/FHD do canal.
- **Canal não abre**: o link pode estar fora do ar. Servidores que exigem User-Agent podem ser configurados com `#EXTVLCOPT:http-user-agent=...` na playlist.
- **Algo estranho aconteceu**: envie o `%APPDATA%\IPTVPlayer\erro.log` para correção.

## Licença do VLC
O VLC é software livre (GPL/LGPL) da VideoLAN. Os arquivos de licença ficam em `vlc\COPYING.txt`.

Use apenas playlists de conteúdo que você tem direito de acessar.
