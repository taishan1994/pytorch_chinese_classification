from pprint import pprint
import sys

sys.path.append('..')
import os
import logging
import shutil
from sklearn.metrics import accuracy_score, f1_score, classification_report
import torch
import torch.nn as nn
import numpy as np
import pickle
import jieba
from torch.utils.data import DataLoader, RandomSampler

from configs import textrnn_bc_config
from preprocess import processor_bc_word
from dataset import dataset_bc_word
from models import textrnn_bc
from utils import utils

logger = logging.getLogger(__name__)


class Trainer:
    def __init__(self, args, train_loader, dev_loader, test_loader):
        self.args = args
        gpu_ids = args.gpu_ids.split(',')
        self.device = torch.device("cpu" if gpu_ids[0] == '-1' else "cuda:" + gpu_ids[0])
        if args.use_pretrained:
            pretrained_embedding = torch.from_numpy(
                pickle.load(open(os.path.join(args.pretrained_dir, args.pretrained_name), 'rb'))).to(torch.float32)
            self.model = textrnn_bc.TextRnn(args, pretrained_embedding)
        else:
            self.model = textrnn_bc.TextRnn(args)
        self.optimizer = torch.optim.AdamW(params=self.model.parameters(), lr=self.args.lr)
        self.criterion = nn.CrossEntropyLoss()
        self.train_loader = train_loader
        self.dev_loader = dev_loader
        self.test_loader = test_loader
        self.model.to(self.device)

    def load_ckp(self, model, optimizer, checkpoint_path):
        checkpoint = torch.load(checkpoint_path)
        model.load_state_dict(checkpoint['state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        epoch = checkpoint['epoch']
        loss = checkpoint['loss']
        return model, optimizer, epoch, loss

    def save_ckp(self, state, checkpoint_path):
        torch.save(state, checkpoint_path)

    """
    def save_ckp(self, state, is_best, checkpoint_path, best_model_path):
        tmp_checkpoint_path = checkpoint_path
        torch.save(state, tmp_checkpoint_path)
        if is_best:
            tmp_best_model_path = best_model_path
            shutil.copyfile(tmp_checkpoint_path, tmp_best_model_path)
    """

    def train(self):
        total_step = len(self.train_loader) * self.args.train_epochs
        global_step = 0
        eval_step = 100
        best_dev_micro_f1 = 0.0
        for epoch in range(args.train_epochs):
            for train_step, train_data in enumerate(self.train_loader):
                self.model.train()
                word_ids = train_data['word_ids'].to(self.device)
                seq_lens = train_data['seq_lens'].to(self.device)
                labels = train_data['labels'].to(self.device)
                train_outputs = self.model(word_ids, seq_lens)
                loss = self.criterion(train_outputs, labels)
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
                logger.info(
                    "【train】 epoch：{} step:{}/{} loss：{:.6f}".format(epoch, global_step, total_step, loss.item()))
                global_step += 1
                if global_step % eval_step == 0:
                    dev_loss, dev_outputs, dev_targets = self.dev()
                    accuracy, micro_f1, macro_f1 = self.get_metrics(dev_outputs, dev_targets)
                    logger.info(
                        "【dev】 loss：{:.6f} accuracy：{:.4f} micro_f1：{:.4f} macro_f1：{:.4f}".format(dev_loss, accuracy,
 micro_f1, macro_f1))
                    if micro_f1 > best_dev_micro_f1:
                        logger.info("------------>保存当前最好的模型")
                        checkpoint = {
                            'epoch': epoch,
                            'loss': dev_loss,
                            'state_dict': self.model.state_dict(),
                            'optimizer': self.optimizer.state_dict(),
                        }
                        best_dev_micro_f1 = micro_f1
                        checkpoint_path = os.path.join(self.args.output_dir, 'textrnn_bc_best.pt')
                        self.save_ckp(checkpoint, checkpoint_path)

    def dev(self):
        self.model.eval()
        total_loss = 0.0
        dev_outputs = []
        dev_targets = []
        with torch.no_grad():
            for dev_step, dev_data in enumerate(self.dev_loader):
                word_ids = dev_data['word_ids'].to(self.device)
                seq_lens = dev_data['seq_lens'].to(self.device)
                labels = dev_data['labels'].to(self.device)
                outputs = self.model(word_ids, seq_lens)
                loss = self.criterion(outputs, labels)
                # val_loss = val_loss + ((1 / (dev_step + 1))) * (loss.item() - val_loss)
                total_loss += loss.item()
                outputs = np.argmax(outputs.cpu().detach().numpy(), axis=1).flatten()
                dev_outputs.extend(outputs.tolist())
                dev_targets.extend(labels.cpu().detach().numpy().tolist())

        return total_loss, dev_outputs, dev_targets

    def test(self, checkpoint_path):
        model = self.model
        optimizer = self.optimizer
        model, optimizer, epoch, loss = self.load_ckp(model, optimizer, checkpoint_path)
        model.eval()
        model.to(self.device)
        total_loss = 0.0
        test_outputs = []
        test_targets = []
        with torch.no_grad():
            for test_step, test_data in enumerate(self.test_loader):
                word_ids = test_data['word_ids'].to(self.device)
                seq_lens = test_data['seq_lens'].to(self.device)
                labels = test_data['labels'].to(self.device)
                outputs = model(word_ids, seq_lens)
                loss = self.criterion(outputs, labels)
                # val_loss = val_loss + ((1 / (dev_step + 1))) * (loss.item() - val_loss)
                total_loss += loss.item()
                outputs = np.argmax(outputs.cpu().detach().numpy(), axis=1).flatten()
                test_outputs.extend(outputs.tolist())
                test_targets.extend(labels.cpu().detach().numpy().tolist())

        return total_loss, test_outputs, test_targets

    def predict(self, tokenizer, text, word2id, id2label, args):
        model = self.model
        optimizer = self.optimizer
        checkpoint = os.path.join(args.output_dir, 'textrnn_bc_best.pt')
        model, optimizer, epoch, loss = self.load_ckp(model, optimizer, checkpoint)
        model.eval()
        model.to(self.device)
        with torch.no_grad():
            word_ids = [word2id.get(word, 1) for word in tokenizer(text, cut_all=False)]
            if len(word_ids) < args.max_seq_len:
                seq_lens = len(word_ids)
                word_ids = word_ids + [0] * (args.max_seq_len - len(word_ids))
            else:
                word_ids = word_ids[:args.max_seq_len]
                seq_lens = args.max_seq_len
            word_ids = torch.from_numpy(np.array([word_ids])).to(self.device)
            seq_lens = torch.from_numpy(np.array([seq_lens])).to(self.device)
            outputs = model(word_ids, seq_lens)
            outputs = np.argmax(outputs.cpu().detach().numpy(), axis=1)
            if len(outputs) != 0:
                outputs = [id2label[i] for i in outputs]
                return outputs
            else:
                return '不好意思，我没有识别出来'

    def get_metrics(self, outputs, targets):
        accuracy = accuracy_score(targets, outputs)
        micro_f1 = f1_score(targets, outputs, average='micro')
        macro_f1 = f1_score(targets, outputs, average='macro')
        return accuracy, micro_f1, macro_f1

    def get_classification_report(self, outputs, targets, labels):
        # confusion_matrix = multilabel_confusion_matrix(targets, outputs)
        report = classification_report(targets, outputs, target_names=labels)
        return report


if __name__ == '__main__':
    args = textrnn_bc_config.Args().get_parser()
    utils.set_seed(args.seed)
    utils.set_logger(os.path.join(args.log_dir, 'textrnn_bc_main.log'))

    processor = processor_bc_word.Processor()

    label2id = {}
    id2label = {}
    with open('../data/cnews/final_data/wiki_word/labels.txt', 'r') as fp:
        labels = fp.read().strip().split('\n')
    for i, label in enumerate(labels):
        label2id[label] = i
        id2label[i] = label
    print(label2id)
    print(id2label)

    word2id = {}
    id2word = {}
    with open('../data/cnews/final_data/wiki_word/vocab.txt', 'r') as fp:
        words = fp.read().strip().split('\n')
    for i, word in enumerate(words):
        word2id[word] = i
        id2word[i] = word

    train_out = dataset_bc_word.get_out(processor, '../data/cnews/raw_data/train.txt', args, word2id, 'train')
    train_features, train_callback_info = train_out
    train_dataset = dataset_bc_word.ClassificationDataset(train_features)
    train_sampler = RandomSampler(train_dataset)
    train_loader = DataLoader(dataset=train_dataset,
                              batch_size=args.train_batch_size,
                              sampler=train_sampler,
                              num_workers=2)

    dev_out = dataset_bc_word.get_out(processor, '../data/cnews/raw_data/dev.txt', args, word2id, 'dev')
    dev_features, dev_callback_info = dev_out
    dev_dataset = dataset_bc_word.ClassificationDataset(dev_features)
    dev_loader = DataLoader(dataset=dev_dataset,
                            batch_size=args.eval_batch_size,
                            num_workers=2)

    test_out = dataset_bc_word.get_out(processor, '../data/cnews/raw_data/test.txt', args, word2id, 'test')
    test_features, test_callback_info = test_out
    test_dataset = dataset_bc_word.ClassificationDataset(test_features)
    test_loader = DataLoader(dataset=test_dataset,
                             batch_size=args.eval_batch_size,
                             num_workers=2)

    trainer = Trainer(args, train_loader, dev_loader, test_loader)
    # 训练和验证
    trainer.train()

    # 测试
    logger.info('========进行测试========')
    checkpoint_path = '../checkpoints/textrnn_bc_best.pt'
    total_loss, test_outputs, test_targets = trainer.test(checkpoint_path)
    accuracy, micro_f1, macro_f1 = trainer.get_metrics(test_outputs, test_targets)
    logger.info(
        "【test】 loss：{:.6f} accuracy：{:.4f} micro_f1：{:.4f} macro_f1：{:.4f}".format(total_loss, accuracy, micro_f1,macro_f1))
    report = trainer.get_classification_report(test_outputs, test_targets, labels)
    logger.info(report)


    # 预测
    trainer = Trainer(args, None, None, None)
    checkpoint_path = '../checkpoints/textrnn_bc_best.pt'
    tokenizer = jieba.lcut
    # 读取test.txt里面的数据
    with open(os.path.join('../data/cnews/raw_data/test_my.txt'), 'r') as fp:
        lines = fp.read().strip().split('\n')
        for line in lines:
            line = line.split('\t')
            text = line[0]
            print(text)
            result = trainer.predict(tokenizer, text, word2id, id2label, args)
            print('真实标签：', id2label[int(line[1])])
            print('预测标签：', result)

    # 预测单条
    text = '8岁男童海螺沟失联13日，父母悬赏10万寻子，马上就到他9岁生日了'
    print(trainer.predict(tokenizer, text, word2id, id2label, args))
