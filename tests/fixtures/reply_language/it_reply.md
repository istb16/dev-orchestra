La revisione è finita. Dei tre revisori, due hanno finito bene e uno è fallito dopo un tempo scaduto. Sono arrivate quattro osservazioni: due accettate, una rifiutata e una unita come duplicato.

- F1 (high): la validazione accettava una stringa al posto di un booleano. Il lato che legge la configurazione tratta tutto ciò che non è falso come attivo, quindi non c'è stato un danno reale, ma il controllo adesso è più severo.
- F2 (medium): lo script che avvolge l'interprete finiva con un errore quando non ne trovava uno adatto. Ora finisce in silenzio, che è quello che ci si aspetta da un hook.

Tutti i test passano di nuovo dopo le correzioni. Non resta niente da fare su questo ramo, e suggerisco di aprire la pull request adesso.
