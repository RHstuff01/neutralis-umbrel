# BTC Sinais Semanais

App para a loja personalizada Neutralis no Umbrel 2.0.1. Mostra BTC/USD, EMA50 semanal, distância até a média e quantidades manuais por rede. Envia alertas no painel e, após configuração, no Telegram.

- Venda: fechamento semanal de cima para baixo da EMA50; cálculo de 70% por rede.
- Recompra: entrada na faixa de 80% a 95% do topo anterior ou fechamento semanal acima da EMA50, o que vier primeiro.
- Semana: fecha domingo 23:59:59 UTC, equivalente a domingo 21h em Brasília.
- Fonte de preço: API pública BTC/USD da Bitstamp. Reconsulta a cada 5 minutos.
- Topo anterior inicial: US$ 69.000, pico Bitstamp de 10/11/2021. Atualizável no painel.
- Posições iniciais: Base 0,2631 cbBTC; Ethereum 0,0256 cbBTC; BNB Chain 0,0439 BTCB. Solana sem quantidade confirmada. Todas podem ser alteradas no painel.

A instalação pela loja Umbrel usa a imagem `ghcr.io/rhstuff01/neutralis-btc-alerta:0.1.1`. O app é somente informativo e não conecta carteiras nem movimenta fundos. Os dados persistentes, inclusive eventual token do Telegram, ficam no volume local da Umbrel.
