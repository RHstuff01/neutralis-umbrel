# Sinais Semanais BTC, ETH e SOL

App para a loja personalizada Neutralis no Umbrel 2.0.1. Mostra preço, EMA50 semanal, distância até a média e quantidades manuais por rede em abas separadas para BTC, ETH e SOL. Envia alertas no painel e, após configuração, no Telegram.

- Venda: fechamento semanal de cima para baixo da EMA50; cálculo de 70% por rede, para cada ativo.
- Recompra: entrada na faixa de 80% a 95% do topo anterior do ativo ou fechamento semanal acima da EMA50, o que vier primeiro.
- Semana: fecha domingo 23:59:59 UTC, equivalente a domingo 21h em Brasília.
- Fonte de preço: API pública da Bitstamp, pares BTC/USD, ETH/USD e SOL/USD. Reconsulta a cada 5 minutos.
- Topos anteriores iniciais: BTC US$ 69.000; ETH máximo Bitstamp entre maio de 2020 e dezembro de 2022; SOL US$ 260, pois o histórico SOL/USD da Bitstamp não cobre todo o pico de 2021. Todos podem ser editados.
- Posições iniciais BTC: Base 0,2631 cbBTC; Ethereum 0,0256 cbBTC; BNB Chain 0,0439 BTCB. Solana sem quantidade confirmada. ETH e SOL começam sem quantidades. Todas podem ser alteradas no painel.

A instalação pela loja Umbrel usa a imagem `ghcr.io/rhstuff01/neutralis-btc-alerta:0.2.1`. O app é somente informativo e não conecta carteiras nem movimenta fundos. Os dados persistentes, inclusive eventual token do Telegram, ficam no volume local da Umbrel.
