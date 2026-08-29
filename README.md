# Neutralis Hedge para Umbrel

Monitor local em **dry-run ou modo real opcional** para uma LP RWA/USD na Byreal,
Raydium ou Orca protegida por um perpétuo HIP-3 na Hyperliquid.

O modo real fica desligado após cada inicialização e exige API Wallet armazenada
localmente e confirmação textual exata. Ao ativar o modo real, o robô corrige
automaticamente o delta inicial quando o residual for negociável.

## Instalação inicial por SSH

Esta é a forma indicada para validar a primeira versão antes de publicar uma
imagem multi-arquitetura em uma Community App Store.

1. Copie a pasta `umbrel-neutralis` para o Umbrel usando o usuário SSH que você
   configurou no aparelho.
2. Entre no Umbrel por SSH e abra a pasta copiada.
3. Execute:

   ```bash
   chmod +x install.sh status.sh stop.sh
   ./install.sh
   ```

4. Abra `http://umbrel.local:8787` em um navegador da mesma rede.

Se `umbrel.local` não resolver, use o endereço IP local do Umbrel seguido de
`:8787`.

Não encaminhe a porta `8787` no roteador e não exponha este painel diretamente
à internet. A instalação manual foi projetada para uso dentro da rede local.

## Operação

1. Escolha Byreal, Raydium ou Orca e confirme as contas públicas mostradas no painel.
2. Na Raydium, cole o endereço do NFT da posição; na Byreal ou Orca, informe a carteira Solana. A descoberta da Orca aceita posições clássicas e Token-2022.
3. Clique em **Buscar LPs**.
4. Selecione a posição desejada.
5. Defina o limite máximo em dólares para o short real. O padrão conservador é US$ 600; o aplicativo nunca aumenta esse valor sozinho.
6. Clique em **Iniciar dry-run**.
7. Acompanhe os ajustes virtuais, a conversão LP → HYP e a divergência no registro.
8. Somente depois de validar o mercado e os valores, informe a confirmação
   mostrada pelo painel para ativar o monitor real.

O container volta automaticamente após uma reinicialização do Umbrel.

## Comandos úteis

```bash
./status.sh
./stop.sh
docker compose -f compose.yaml ps
docker compose -f compose.yaml logs -f neutralis
docker compose -f compose.yaml restart neutralis
docker compose -f compose.yaml stop neutralis
```

## Proteções

- O modo real inicia sempre desligado e exige confirmação manual exata.
- A chave privada da API Wallet fica somente no volume local `/data`.
- No modo real, o delta inicial é corrigido antes de o preço virar âncora.
- No dry-run, nenhuma ordem inicial ou posterior é enviada.
- Ajustes ocorrem somente depois de um movimento de 0,5% desde a âncora.
- Compras são `reduce-only` e nunca podem abrir uma posição long.
- Ordens automáticas são IOC, têm até três tentativas para o residual e respeitam o limite total configurado pelo usuário (US$ 600 por padrão).
- O processo roda sem privilégios, sem capabilities Linux e com sistema de
  arquivos somente leitura, exceto `/data`.
- Configuração e eventos públicos persistem em `./data`.
- O monitor pausa se houver posição long, ordens abertas, alteração da posição
  real, saída da faixa ou divergência de mark/oráculo superior a 0,75%.
- Para wrappers 1:1 como CRCLx, o monitor pausa se o preço da LP divergir mais de 0,75% do perp correspondente na Hyperliquid.
- Para SPYx, o hedge usa `xyz:SP500` por **valor nocional**: quantidade de SPYx × preço da LP ÷ preço do SP500. O robô monitora a variação da relação entre os dois preços e pausa se ela se afastar mais de 0,75% da relação observada ao iniciar.
- SPYx e SP500 não são o mesmo ativo. O hedge reduz o delta em dólares, mas permanece sujeito a risco de base entre o ETF/token e o índice.
- Pares sem cotação estável reconhecida, como Fartcoin/SOL, não são elegíveis para este monitor de RWA/USD.

## Community App Store

`umbrel-app.yml` e `docker-compose.yml` são um esqueleto para a instalação
nativa. Antes de adicioná-lo a uma loja, é necessário publicar a imagem
`neutralis-umbrel:0.5.3` para ARM64 e AMD64 em um registry e substituir a
diretiva `build` por esse endereço de imagem.
