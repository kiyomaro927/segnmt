import argparse
from logging import getLogger
from pathlib import Path
from typing import Callable
from typing import Dict
from typing import List
from typing import NamedTuple
from typing import Optional
from typing import Tuple
from typing import Union

import chainer
from chainer.dataset import to_device
import chainer.functions as F
from chainer import training
from chainer.training import extensions
from chainer import Variable
import matplotlib
from nltk.translate import bleu_score
import numpy as np
from progressbar import ProgressBar

from segnmt.misc.constants import EOS
from segnmt.misc.constants import PAD
from segnmt.misc.constants import UNK
from segnmt.misc.typing import ndarray
from segnmt.models.encdec import EncoderDecoder


logger = getLogger(__name__)
matplotlib.use('Agg')


class ConstArguments(NamedTuple):
    # Encoder-Decoder arguments
    source_vocabulary_size: int
    source_word_embeddings_size: int
    encoder_hidden_layer_size: int
    encoder_num_steps: int
    encoder_dropout: float
    target_vocabulary_size: int
    target_word_embeddings_size: int
    decoder_hidden_layer_size: int
    attention_hidden_layer_size: int
    maxout_layer_size: int

    gpu: int
    minibatch_size: int
    epoch: int
    source_vocab: str
    target_vocab: str
    training_source: str
    training_target: str
    validation_source: Optional[str]
    validation_target: Optional[str]
    similar_sentence_indices: Optional[str]
    similar_sentence_indices_validation: Optional[str]
    loss_plot_file: str
    bleu_plot_file: str
    resume_file: Optional[str]
    extension_trigger: int


class CalculateBleu(chainer.training.Extension):
    triger = (1, 'epoch')
    priority = chainer.training.PRIORITY_WRITER

    def __init__(
            self,
            validation_iter: chainer.iterators.SerialIterator,
            model: EncoderDecoder,
            converter: Callable[
                [List[Tuple[np.ndarray, np.ndarray]], Optional[int]],
                Tuple[ndarray, ndarray]
            ],
            key: str,
            device: int
    ):
        self.iter = validation_iter
        self.model = model
        self.converter = converter
        self.device = device
        self.key = key

    def __call__(self, trainer):
        list_of_references = []
        hypotheses = []
        self.iter.reset()
        with chainer.no_backprop_mode(), chainer.using_config('train', False):
            for minibatch in self.iter:
                target_sentences: List[np.ndarray] = tuple(zip(*minibatch))[1]
                list_of_references.extend(
                    [[sentence.tolist()] for sentence in target_sentences]
                )
                source, _ = self.converter(minibatch, self.device)
                results = self.model.translate(source)
                hypotheses.extend(
                    # Remove <EOS>
                    [sentence.tolist()[:-1] for sentence in results]
                )
        bleu = bleu_score.corpus_bleu(
            list_of_references,
            hypotheses,
            smoothing_function=bleu_score.SmoothingFunction().method1
        )
        chainer.report({self.key: bleu})


def convert(
        minibatch: List[Tuple[np.ndarray, np.ndarray]],
        device: Optional[int]
) -> Tuple[ndarray, ndarray]:
    # Append eos to the end of sentence
    eos = np.array([EOS], 'i')
    src_batch, tgt_batch = zip(*minibatch)
    with chainer.no_backprop_mode():
        src_sentences = \
            [Variable(np.hstack((sentence, eos))) for sentence in src_batch]
        tgt_sentences = \
            [Variable(np.hstack((sentence, eos))) for sentence in tgt_batch]

        src_block = F.pad_sequence(src_sentences, padding=PAD).data
        tgt_block = F.pad_sequence(tgt_sentences, padding=PAD).data

    return (
        to_device(device, src_block),
        to_device(device, tgt_block)
    )


def convert_with_similar_sentences(
        minibatch: List[
            Tuple[np.ndarray, np.ndarray, List[Tuple[np.ndarray, np.ndarray]]]
        ],
        device: Optional[int]
) -> Tuple[ndarray, ndarray, List[Tuple[ndarray, ndarray]]]:
    src_batch, tgt_batch, sim_batches = zip(*minibatch)
    source, target = convert(list(zip(src_batch, tgt_batch)), device)
    # len(sim_batches) == minibatch_size
    # len(sim_batches[i]) == retrieved_size
    # sim_batches[i][j][0].shape == (source_sentence_size,)
    # sim_batches[i][j][1].shape == (target_sentence_size,)
    max_retrieved_count = max(len(x) for x in sim_batches)
    similar_sentences = []
    for i in range(max_retrieved_count):
        similar_sentences.append(
            convert([
                sim_batch[i] if i < len(sim_batch)
                else (np.array([], 'i'), np.array([], 'i'))
                for sim_batch in sim_batches
            ], device)
        )

    return (
        source, target, similar_sentences
    )


def load_vocab(vocab_file: Union[Path, str], size: int) -> Dict[str, int]:
    """Create a vocabulary from a file.

    The file specified by `vocab` must be contain one word per line.
    """

    if isinstance(vocab_file, str):
        vocab_file = Path(vocab_file)
    assert vocab_file.exists()

    words = ['<UNK>', '<EOS>']
    with open(vocab_file) as f:
        words += [line.strip() for line in f]
    assert size <= len(words)

    vocab = {word: index for index, word in enumerate(words) if index < size}
    assert vocab['<UNK>'] == UNK
    assert vocab['<EOS>'] == EOS

    return vocab


def load_data(
        source: Union[Path, str],
        target: Union[Path, str],
        source_vocab: Dict[str, int],
        target_vocab: Dict[str, int],
        similar_index: Optional[Union[Path, str]] = None
) -> Union[
    List[Tuple[np.ndarray, np.ndarray]],
    List[Tuple[np.ndarray, np.ndarray, List[Tuple[np.ndarray, np.ndarray]]]]
]:
    if isinstance(source, str):
        source = Path(source)
    if isinstance(target, str):
        target = Path(target)
    assert source.exists()
    assert target.exists()

    data = []

    with open(source) as src, open(target) as tgt:
        src_len = sum(1 for _ in src)
        tgt_len = sum(1 for _ in tgt)
        assert src_len == tgt_len
        file_len = src_len

    logger.info(f'loading {source.absolute()} and {target.absolute()}')
    with open(source) as src, open(target) as tgt:
        bar = ProgressBar()
        i = 0
        for i, (s, t) in bar(enumerate(zip(src, tgt)), max_value=file_len):
            s_words = s.strip().split()
            t_words = t.strip().split()
            s_array = \
                np.array([source_vocab.get(w, UNK) for w in s_words], 'i')
            t_array = \
                np.array([target_vocab.get(w, UNK) for w in t_words], 'i')
            data.append((s_array, t_array))

    return data


def load_train_data(
        source: Union[Path, str],
        target: Union[Path, str],
        source_vocab: Dict[str, int],
        target_vocab: Dict[str, int],
        similar_indices: Union[Path, str]
) -> List[Tuple[np.ndarray, np.ndarray, List[Tuple[np.ndarray, np.ndarray]]]]:
    if isinstance(source, str):
        source = Path(source)
    if isinstance(target, str):
        target = Path(target)
    if isinstance(similar_indices, str):
        similar_indices = Path(similar_indices)
    assert source.exists()
    assert target.exists()
    assert similar_indices.exists()

    data = load_data(source, target, source_vocab, target_vocab)

    with open(similar_indices) as sim:
        assert len(data) == sum(1 for _ in sim)

    fulldata = []

    logger.info(f'loading similar sentences from {similar_indices.absolute()}')
    with open(similar_indices) as f:
        bar = ProgressBar()
        for i, line in bar(enumerate(f), max_value=len(data)):
            indices = [int(i) for i in line.strip().split()][1:]
            similar_data = [data[index] for index in indices]
            fulldata.append((*data[i], similar_data))

    return fulldata


def load_validation_data(
        train_source: Union[Path, str],
        train_target: Union[Path, str],
        source: Union[Path, str],
        target: Union[Path, str],
        source_vocab: Dict[str, int],
        target_vocab: Dict[str, int],
        similar_indices: Union[Path, str]
) -> List[Tuple[np.ndarray, np.ndarray, List[Tuple[np.ndarray, np.ndarray]]]]:
    if isinstance(train_source, str):
        train_source = Path(train_source)
    if isinstance(train_target, str):
        train_target = Path(train_target)
    if isinstance(source, str):
        source = Path(source)
    if isinstance(target, str):
        target = Path(target)
    if isinstance(similar_indices, str):
        similar_indices = Path(similar_indices)
    assert train_source.exists()
    assert train_target.exists()
    assert source.exists()
    assert target.exists()
    assert similar_indices.exists()

    train_data = load_data(
        train_source, train_target,
        source_vocab, target_vocab
    )

    data = load_data(source, target, source_vocab, target_vocab)

    with open(similar_indices) as sim:
        assert len(data) == sum(1 for _ in sim)

    fulldata = []

    logger.info(f'loading similar sentences from {similar_indices.absolute()}')
    with open(similar_indices) as f:
        bar = ProgressBar()
        for i, line in bar(enumerate(f), max_value=len(data)):
            indices = [int(i) for i in line.strip().split()][1:]
            similar_data = [train_data[index] for index in indices]
            fulldata.append((*data[i], similar_data))

    return fulldata


def train(args: argparse.Namespace):
    cargs = ConstArguments(**vars(args))
    logger.info(f'cargs: {cargs}')
    model = EncoderDecoder(cargs.source_vocabulary_size,
                           cargs.source_word_embeddings_size,
                           cargs.encoder_hidden_layer_size,
                           cargs.encoder_num_steps,
                           cargs.encoder_dropout,
                           cargs.target_vocabulary_size,
                           cargs.target_word_embeddings_size,
                           cargs.decoder_hidden_layer_size,
                           cargs.attention_hidden_layer_size,
                           cargs.maxout_layer_size)
    if cargs.gpu >= 0:
        chainer.cuda.get_device_from_id(cargs.gpu).use()
        model.to_gpu(cargs.gpu)

    optimizer = chainer.optimizers.Adam()
    optimizer.setup(model)

    source_vocab = load_vocab(cargs.source_vocab, cargs.source_vocabulary_size)
    target_vocab = load_vocab(cargs.target_vocab, cargs.target_vocabulary_size)

    training_data = load_train_data(
        cargs.training_source,
        cargs.training_target,
        source_vocab,
        target_vocab,
        cargs.similar_sentence_indices
    )

    training_iter = chainer.iterators.SerialIterator(training_data,
                                                     cargs.minibatch_size)
    converter = convert
    if cargs.similar_sentence_indices is not None:
        converter = convert_with_similar_sentences
    updater = training.StandardUpdater(
        training_iter, optimizer, converter=converter, device=cargs.gpu)
    trainer = training.Trainer(updater, (cargs.epoch, 'epoch'))
    trainer.extend(extensions.LogReport(
        trigger=(cargs.extension_trigger, 'iteration')
    ))
    trainer.extend(
        extensions.PrintReport(
            ['epoch', 'iteration', 'main/loss', 'validation/main/loss',
             'validation/main/bleu', 'elapsed_time']
        ),
        trigger=(cargs.extension_trigger, 'iteration')
    )
    trainer.extend(
        extensions.snapshot(),
        trigger=(cargs.extension_trigger * 50, 'iteration'))
    # Don't set `trigger` argument to `dump_graph`
    trainer.extend(extensions.dump_graph('main/loss'))
    if extensions.PlotReport.available():
        trainer.extend(extensions.PlotReport(
            ['main/loss', 'validation/main/loss'],
            trigger=(cargs.extension_trigger, 'iteration'),
            file_name=cargs.loss_plot_file
        ))
        trainer.extend(extensions.PlotReport(
            ['validation/main/bleu'],
            trigger=(cargs.extension_trigger, 'iteration'),
            file_name=cargs.bleu_plot_file
        ))
    else:
        logger.warning('PlotReport is not available.')

    if cargs.validation_source is not None and \
            cargs.validation_target is not None:
        validation_data = load_validation_data(
            cargs.training_source,
            cargs.training_target,
            cargs.validation_source,
            cargs.validation_target,
            source_vocab,
            target_vocab,
            cargs.similar_sentence_indices_validation
        )

        v_iter1 = chainer.iterators.SerialIterator(
            validation_data,
            cargs.minibatch_size,
            repeat=False,
            shuffle=False
        )
        v_iter2 = chainer.iterators.SerialIterator(
            validation_data,
            cargs.minibatch_size,
            repeat=False,
            shuffle=False
        )

        trainer.extend(extensions.Evaluator(
            v_iter1, model, converter=converter, device=cargs.gpu
        ), trigger=(cargs.extension_trigger * 5, 'iteration'))
        trainer.extend(CalculateBleu(
            v_iter2, model, converter=convert, device=cargs.gpu,
            key='validation/main/bleu'
        ), trigger=(cargs.extension_trigger * 5, 'iteration'))

        source_word = {index: word for word, index in source_vocab.items()}
        target_word = {index: word for word, index in target_vocab.items()}

        validation_size = len(validation_data)

        @chainer.training.make_extension(trigger=(200, 'iteration'))
        def translate(_):
            data = validation_data[np.random.choice(validation_size)]
            converted = converter([data], cargs.gpu)
            source, target = converted[:2]
            result = model.translate(source)[0].reshape((1, -1))

            source_sentence = ' '.join(
                [source_word[int(word)] for word in source[0]]
            )
            target_sentence = ' '.join(
                [target_word[int(word)] for word in target[0]]
            )
            result_sentence = ' '.join(
                [target_word[int(word)] for word in result[0]]
            )
            logger.info('# source : ' + source_sentence)
            logger.info('# result : ' + result_sentence)
            logger.info('# expect : ' + target_sentence)

        trainer.extend(
            translate,
            trigger=(cargs.extension_trigger * 5, 'iteration')
        )

    trainer.extend(
        extensions.ProgressBar(
            update_interval=max(cargs.extension_trigger // 5, 1)
        )
    )

    if cargs.resume_file is not None:
        chainer.serializers.load_npz(cargs.resume_file, trainer)

    print('start training')

    trainer.run()
