A revisão já terminou. Dos três revisores, dois terminaram bem e um falhou depois de um tempo esgotado. Chegaram quatro observações: duas foram aceitas, uma foi rejeitada e outra foi unida como duplicata.

- F1 (high): a validação aceitava uma cadeia no lugar de um booleano. O lado que lê a configuração trata tudo que não é falso como ativado, então não houve dano real, mas a verificação agora é mais rígida.
- F2 (medium): o script que envolve o interpretador terminava com um erro quando não encontrava um adequado. Agora ele termina sem barulho, que é o que se espera de um hook.

Todos os testes passam de novo depois das correções. Não há mais nada a fazer nesta branch, e eu sugiro abrir a pull request agora.
