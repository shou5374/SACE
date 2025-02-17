'''
This source code is licensed under the license found in the
LICENSE file in the root directory of this source tree.
'''

import os
import re
import torch
import subprocess
from pytorch_transformers import *
import random
from bs4 import BeautifulSoup
from nltk.corpus import wordnet as wn
from tqdm import tqdm
from transformers import AutoTokenizer, AutoModel

# pos_converter = {'NOUN':'n', 'PROPN':'n', 'VERB':'v', 'AUX':'v', 'ADJ':'a', 'ADV':'r'}
pos_converter = {
    # U-POS
    "NOUN": "n",
    "VERB": "v",
    "ADJ": "a",
    "ADV": "r",
    "PROPN": "n",
    # PEN
    "AFX": "a",
    "JJ": "a",
    "JJR": "a",
    "JJS": "a",
    "MD": "v",
    "NN": "n",
    "NNP": "n",
    "NNPS": "n",
    "NNS": "n",
    "RB": "r",
    "RP": "r",
    "RBR": "r",
    "RBS": "r",
    "VB": "v",
    "VBD": "v",
    "VBG": "v",
    "VBN": "v",
    "VBP": "v",
    "VBZ": "v",
    "WRB": "r",
    "PRT": "r",
}

def generate_key(lemma, pos):
    if pos in pos_converter.keys():
        pos = pos_converter[pos]
    key = '{}+{}'.format(lemma, pos)
    return key

def load_pretrained_model(name):
    if name == 'roberta-base':
        # model = RobertaModel.from_pretrained('roberta-base')
        model = RobertaModel.from_pretrained('roberta-base', output_hidden_states=True)
        hdim = 768
    elif name == 'roberta-large':
        # model = RobertaModel.from_pretrained('roberta-large')
        model = RobertaModel.from_pretrained('roberta-large', output_hidden_states=True)
        hdim = 1024
    elif name == 'xlmroberta-base':
        model = AutoModel.from_pretrained("xlm-roberta-base", output_hidden_states=True)
        hdim = 768
    elif name == 'xlmroberta-large':
        model = AutoModel.from_pretrained("xlm-roberta-large", output_hidden_states=True)
        hdim = 1024
    elif name == 'bert-large':
        model = BertModel.from_pretrained('bert-large-cased', output_hidden_states=True)
        hdim = 1024
    else: #bert base
        model = BertModel.from_pretrained('bert-base-cased', output_hidden_states=True)
        hdim = 768
    return model, hdim

def load_tokenizer(name):
    if name == 'roberta-base':
        tokenizer = RobertaTokenizer.from_pretrained('roberta-base')
    elif name == 'roberta-large':
        tokenizer = RobertaTokenizer.from_pretrained('roberta-large')
    elif name == 'xlmroberta-base':
        tokenizer = AutoTokenizer.from_pretrained("xlm-roberta-base")
    elif name == 'xlmroberta-large':
        tokenizer = AutoTokenizer.from_pretrained("xlm-roberta-large")
    elif name == 'bert-large':
        tokenizer = BertTokenizer.from_pretrained('bert-large-cased')
    else: #bert base
        tokenizer = BertTokenizer.from_pretrained('bert-base-cased')
    return tokenizer

def load_wn_senses(path):
    wn_senses = {}
    with open(path, 'r', encoding="utf8") as f:
        for line in f:
            line = line.strip().split('\t')
            lemma = line[0]
            pos = line[1]
            senses = line[2:]

            key = generate_key(lemma, pos)
            wn_senses[key] = senses
    return wn_senses

def get_label_space(data):
    #get set of labels from dataset
    labels = set()

    for sent in data:
        for _, _, _, _, label in sent:
            if label != -1:
                labels.add(label)

    labels = list(labels)
    labels.sort()
    labels.append('n/a')

    label_map = {}
    for sent in data:
        for _, lemma, pos, _, label in sent:
            if label != -1:
                key = generate_key(lemma, pos)
                label_idx = labels.index(label)
                if key not in label_map: label_map[key] = set()
                label_map[key].add(label_idx)

    return labels, label_map

def process_encoder_outputs(output, mask, as_tensor=False):
    combined_outputs = []
    position = -1
    avg_arr = []
    for idx, rep in zip(mask, torch.split(output, 1, dim=0)):
        #ignore unlabeled words
        if idx == -1: continue
        #average representations for units in same example
        elif position < idx:
            position=idx
            if len(avg_arr) > 0: combined_outputs.append(torch.mean(torch.stack(avg_arr, dim=-1), dim=-1))
            avg_arr = [rep]
        else:
            assert position == idx
            avg_arr.append(rep)
    #get last example from avg_arr
    if len(avg_arr) > 0: combined_outputs.append(torch.mean(torch.stack(avg_arr, dim=-1), dim=-1))
    if as_tensor: return torch.cat(combined_outputs, dim=0)
    else: return combined_outputs

#run WSD Evaluation Framework scorer within python
def evaluate_output(scorer_path, gold_filepath, out_filepath):
    eval_cmd = ['java','-cp', scorer_path, 'Scorer', gold_filepath, out_filepath]
    output = subprocess.Popen(eval_cmd, stdout=subprocess.PIPE ).communicate()[0]
    output = [x.decode("utf-8") for x in output.splitlines()]
    p, r, f1 = [float(output[i].split('=')[-1].strip()[:-1]) for i in range(3)]
    return p, r, f1

def get_adj_keys():
    key_list = []
    for synset in wn.all_synsets('a'):
        for lemma in synset.lemmas():
            key_list.extend([lemma.key()])
    return key_list

def load_data(datapath, name, train_sent=None):
    if 'wngt' in name:
        name, new_name = name.split('-')
    else:
        name, new_name = name, ''
    text_path = os.path.join(datapath, '{}.data.xml'.format(name))
    gold_path = os.path.join(datapath, '{}.gold.key.txt'.format(name))

    #load gold labels
    gold_labels = {}
    with open(gold_path, 'r', encoding="utf8") as f:
        for line in f:
            line = line.strip().split(' ')
            instance = line[0]
            #this means we are ignoring other senses if labeled with more than one
            #(happens at least in SemCor data)
            key = line[1]
            gold_labels[instance] = key

    #load train examples + annotate sense instances with gold labels
    sentences = []
    s = []
    with open(text_path, 'r', encoding="utf8") as f:
        for line in f:
            line = line.strip()
            if line == '</sentence>':
                sentences.append(s)
                s=[]
                if 'semcor' in name and len(sentences) >= train_sent:
                    break

            elif line.startswith('<instance') or line.startswith('<wf'):
                word = re.search('>(.+?)<', line).group(1)
                # print(line)
                try:
                    lemma = re.search('lemma="(.+?)"', line).group(1)
                except AttributeError:
                    lemma = word.lower()
                pos = re.search('pos="(.+?)"', line).group(1)

                #clean up data
                word = re.sub('&apos;', '\'', word)
                lemma = re.sub('&apos;', '\'', lemma).lower()

                sense_inst = -1
                sense_label = -1
                if line.startswith('<instance'):
                    sense_inst = re.search('instance id="(.+?)"', line).group(1)
                    #annotate sense instance with gold label
                    sense_label = gold_labels.get(sense_inst)
                    sense_label = sense_label if sense_label else -1
                s.append((word, lemma, pos, sense_inst, sense_label))
    if new_name and 'semcor' in name:
        # sent_num = 0
        extra_path = os.path.join(datapath, '{}.xml'.format(new_name))
        wngt_corpus = open(extra_path, 'r').read()
        wsd_bs = BeautifulSoup(wngt_corpus, 'xml')
        text_all = wsd_bs.find_all('sentence')
        type2pos = {'j': 'ADJ', 'n': 'NOUN', 'r': 'ADV', 'v': 'VERB'}

        adj_keys = get_adj_keys()
        num = 0
        for sent in tqdm(text_all[:]):
            s = []
            for word in sent.find_all('word'):
                w = word['surface_form'].replace('_', ' ')
                lemma = word['lemma'] if 'lemma' in word.attrs else word['surface_form'].replace('_', ' ')
                pos = type2pos[word['pos'][0].lower()] if word['pos'][0].lower() in type2pos else word['pos']
                key = word['wn30_key'].split(';')[0] if 'wn30_key' in word.attrs else -1
                if key != -1 and key not in adj_keys and '%3:' in key:
                    pos_string = key.split('%')[1][0]
                    replace_string = '35'.replace(key.split('%')[1][0], '')
                    key = key.replace('%' + pos_string + ':', '%' + replace_string + ':')
                sense_inst = 'd0.s%d.t0' % num if key != -1 else -1
                s.append((w, lemma, pos, sense_inst, key))
            num += 1
            sentences.append(s)

    return sentences

#normalize ids list, masks to whatever the passed in length is
def normalize_length(ids, attn_mask, o_mask, max_len, pad_id):
    if max_len == -1:
        return ids, attn_mask, o_mask
    else:
        if len(ids) < max_len:
            while len(ids) < max_len:
                ids.append(torch.tensor([[pad_id]]))
                attn_mask.append(0)
                o_mask.append(-1)
        else:
            ids = ids[:max_len-1]+[ids[-1]]
            attn_mask = attn_mask[:max_len]
            o_mask = o_mask[:max_len]

        assert len(ids) == max_len
        assert len(attn_mask) == max_len
        assert len(o_mask) == max_len

        return ids, attn_mask, o_mask

#filters down training dataset to (up to) k examples per sense 
#for few-shot learning of the model
def filter_k_examples(data, k):
    #shuffle data so we don't only get examples for (common) senses from beginning
    random.shuffle(data)
    #track number of times sense from data is used
    sense_dict = {}
    #store filtered data
    filtered_data = []

    example_count = 0
    for sent in data:
        filtered_sent = []
        for form, lemma, pos, inst, sense in sent:
            #treat unlabeled words normally
            if sense == -1:
                x  = (form, lemma, pos, inst, sense)
            elif sense in sense_dict:
                if sense_dict[sense] < k:
                    #increment sense count and add example to filtered data
                    sense_dict[sense] += 1
                    x = (form, lemma, pos, inst, sense)
                    example_count += 1
                else: #if the data already has k examples of this sense
                    #add example with no instance or sense label to data
                    x = (form, lemma, pos, -1, -1)
            else:
                #add labeled example to filtered data and sense dict
                sense_dict[sense] = 1
                x = (form, lemma, pos, inst, sense)
                example_count += 1
            filtered_sent.append(x)
        filtered_data.append(filtered_sent)

    print("k={}, training on {} sense examples...".format(k, example_count))

    return filtered_data

#EOF