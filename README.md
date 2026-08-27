# Neutralis Hedge para Umbrel

Monitor local em **dry-run** para uma LP RWA/USD na Byreal protegida por um
perpétuo HIP-3 na Hyperliquid.

Esta versão não contém chave privada, biblioteca de assinatura ou chamada ao
endpoint de negociação. Ela apenas lê dados públicos e registra as operações
que simularia.

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

1. Confirme as duas carteiras públicas mostradas no painel.
2. Clique em **Buscar LPs**.
3. Selecione a posição desejada.
4. Clique em **Iniciar dry-run**.
5. Acompanhe os ajustes virtuais no registro.

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

- `ordersEnabled` permanece sempre `false`.
- Nenhuma chave privada é aceita pelo painel.
- Nenhuma chamada é feita ao endpoint de negociação da Hyperliquid.
- O processo roda sem privilégios, sem capabilities Linux e com sistema de
  arquivos somente leitura, exceto `/data`.
- Configuração e eventos públicos persistem em `./data`.
- O monitor pausa se houver posição long, ordens abertas, alteração da posição
  real, saída da faixa ou divergência de mark/oráculo superior a 0,75%.

## Community App Store

`umbrel-app.yml` e `docker-compose.yml` são um esqueleto para a instalação
nativa. Antes de adicioná-lo a uma loja, é necessário publicar a imagem
`neutralis-umbrel:0.1.0` para ARM64 e AMD64 em um registry e substituir a
diretiva `build` por esse endereço de imagem.
