python biencoder-context.py --gloss-bsz 150 --epoch 10 --gloss_max_length 48 --step_mul 50 --warmup 10000 --gloss_mode sense-pred --lr 1e-6 --word non --encoder-name roberta-large --train_mode roberta-large --context_len 2 --train_data semcor-wngt --same