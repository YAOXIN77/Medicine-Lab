import time
from collections import defaultdict
from tqdm import trange
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import src.py.training_on_the_fly.eval_val_3D as eval_val
import src.py.utils.one_hot_embedding as one_hot
from src.py.utils.visualize_batch import visualize_LR
import src.py.utils.mkdir as mkdir
import torch.optim as optim
from src.py.utils.loss import DiceCoefficientLF
from src.py.networks.LR_3D import *
from src.py.networks.GAN_3D import *
import random

from time import time
import numpy as np
from batchgenerators.augmentations.crop_and_pad_augmentations import crop
from src.py.utils.vis_loss import history_log, visualize_loss
from batchgenerators.dataloading.multi_threaded_augmenter import MultiThreadedAugmenter
from batchgenerators.dataloading.data_loader import DataLoader
from batchgenerators.transforms.spatial_transforms import SpatialTransform_2, MirrorTransform
from batchgenerators.transforms.color_transforms import BrightnessMultiplicativeTransform, GammaTransform
from batchgenerators.utilities.file_and_folder_operations import *
from batchgenerators.transforms.abstract_transforms import Compose
from skimage.util import view_as_blocks
from copy import deepcopy

from collections import OrderedDict
from src.py.preprocessing.airway_error_generator import RandomAirwayErrorGenerator


def poly_lr(epoch, max_epochs, initial_lr, exponent=0.9):
    return initial_lr * (1 - epoch / max_epochs)**exponent


def add_random_error(seg, lower_bound_ratio, upper_bound_ratio, error_size):
    ratio = np.around(np.random.uniform(lower_bound_ratio, upper_bound_ratio), decimals=10)
    error_size_int = 2
    error_size = (error_size_int, error_size_int, error_size_int)
    seg_error = modify_patches(seg, error_size, lambda x: 1, ratio)
    return seg_error, ratio


def modify_patches(img, patch_shape, fn_to_apply, max_fraction_masked):

    patches = deepcopy(view_as_blocks(img, block_shape=patch_shape))

    # if in the grid, the number is 1, we mask the corresponding patch
    if len(img.shape) == 3:
        masking_grid = biased_flip(max_fraction_masked,
                                   shape=(int((img.shape[0] / patch_shape[0])),
                                          int((img.shape[1] / patch_shape[1])),
                                          int((img.shape[2] / patch_shape[2]))))
        for i in range(masking_grid.shape[0]):
            for j in range(masking_grid.shape[1]):
                for k in range(masking_grid.shape[2]):
                    # print(i, j, k)
                    if masking_grid[i, j, k] == 1:
                        patches[i, j, k, :, :, :] = fn_to_apply(patches[i, j, k, :, :, :])

        patches = patches.transpose(0, 3, 1, 4, 2, 5)
        return np.reshape(patches, img.shape)
    elif len(img.shape) == 2:
        masking_grid = biased_flip(max_fraction_masked,
                                   shape=(int((img.shape[0] / patch_shape[0])),
                                          int((img.shape[1] / patch_shape[1]))))
        for i in range(masking_grid.shape[0]):
            for j in range(masking_grid.shape[1]):
                if masking_grid[i, j] == 1:
                    patches[i, j, :, :] = fn_to_apply(patches[i, j, :, :])

        patches = patches.transpose(0, 2, 1, 3)
        return np.reshape(patches, img.shape)


def biased_flip(p, shape):
    temp = np.random.random(shape)
    return np.where(temp <= p, 1, 0)


class AirwayDataLoader3D(DataLoader):
    def __init__(self, data, data_initial, batchsize, patch_size, num_threads_in_multithreaded, job_description, seed_for_shuffle=1234, reture_incomplete=False, shuffle=True, crop='random', modalities=1, mode='train',
                 is_generates_errors_labels=False, path_basedir_info_gen_errors=''):
        super().__init__(data, batchsize, num_threads_in_multithreaded, seed_for_shuffle, reture_incomplete, shuffle, True)
        self.patch_size = patch_size
        self.num_modalities = modalities
        self.indices = list(range(len(data)))
        self.crop = crop
        self.data_initial = data_initial
        self.job_description = job_description
        self.mode = mode

        if job_description['dataset'] == 'airway':
            self._is_generate_errors_labels = is_generates_errors_labels
            self._path_basedir_info_gen_errors = path_basedir_info_gen_errors
            if self._is_generate_errors_labels:
                print("Create Generator of Random Errors in Airway Mask...")
                self.create_data_generate_errors_labels()

        # ----------------

    @staticmethod
    def load_patient_shape(patient):
        data = np.load(patient + '.npy', mmap_mode='r')
        metadata = load_pickle(patient + '.pkl')
        return data, metadata

    @staticmethod
    def load_patient(patient, initial):
        data = np.load(patient + '.npy', mmap_mode='r')
        # print('data: ', data.shape)
        initial_seg = np.expand_dims(np.load(initial + '.npy', mmap_mode='r'), axis=0)
        # print('initial_seg: ', initial_seg.shape)
        data = np.concatenate([data, initial_seg], axis=0)
        metadata = load_pickle(patient + '.pkl')
        return data, metadata

    def create_data_generate_errors_labels(self):
        path_dir_airway_measures = os.path.join(self._path_basedir_info_gen_errors, 'AirwayMeasurements/')
        reference_keys_procimages_file = os.path.join(self._path_basedir_info_gen_errors, 'map/referenceKeys_procimages.npy')
        reference_keys_nnunetimages_file = os.path.join(self._path_basedir_info_gen_errors, 'map/referenceKeys_nnUnetimages.npy')

        reference_keys_procimages = dict(np.load(reference_keys_procimages_file, allow_pickle=True).item())
        reference_keys_nnunetimages = dict(np.load(reference_keys_nnunetimages_file, allow_pickle=True).item())

        dict_air_measures_files = OrderedDict()
        for ipatient in self._data:
            ipatient = os.path.basename(ipatient)
            iprocimage = reference_keys_nnunetimages[ipatient].replace('.nii.gz', '')
            iorigscan_file = reference_keys_procimages[iprocimage]

            if 'crop-01' in iprocimage:
                iair_measures_file = iorigscan_file.replace('.dcm', '_LeftLung_ResultsPerBranch.csv')
                iair_measures_file = os.path.join(path_dir_airway_measures, iair_measures_file)
                dict_air_measures_files[ipatient] = iair_measures_file
            elif 'crop-02' in iprocimage:
                iair_measures_file = iorigscan_file.replace('.dcm', '_RightLung_ResultsPerBranch.csv')
                iair_measures_file = os.path.join(path_dir_airway_measures, iair_measures_file)
                dict_air_measures_files[ipatient] = iair_measures_file
            else:
                print("ERROR: in file \'train_LR_Syn_GAN_3D_onlineError\' and line \'143\'...")
                print("Input \'proc\' image name does not contain \'crop-01\' or \'crop-02\'. Generator of errors "
                      "in airways assumes that we have input images cropped around each lung separately...")
                exit(0)

        self._random_error_generator = RandomAirwayErrorGenerator(dict_air_measures_files,
                                                                  is_error_type1=True, p_branches_error_type1=self.job_description['airway_error_1'],
                                                                  is_error_type2=True, p_branches_error_type2=self.job_description['airway_error_2'])


    def generate_train_batch(self):
        idx = self.get_indices()
        patient_for_batch = [self._data[i] for i in idx]
        patient_for_batch_initial = [self.data_initial[i] for i in idx]

        data = np.zeros((self.batch_size, self.num_modalities, *self.patch_size), dtype=np.float32)

        if self.job_description['dataset'] == 'airway':
            if self._is_generate_errors_labels:
                seg = np.zeros((self.batch_size, 4, *self.patch_size), dtype=np.float32)
            else:
                seg = np.zeros((self.batch_size, 3, *self.patch_size), dtype=np.float32)

        if self.job_description['dataset'] == 'vessel':
            if self._is_generate_errors_labels:
                seg = np.zeros((self.batch_size, 3, *self.patch_size), dtype=np.float32)
            else:
                seg = np.zeros((self.batch_size, 2, *self.patch_size), dtype=np.float32)

        metadata = []
        patient_names = []

        for i, j in enumerate(patient_for_batch):
            k = patient_for_batch_initial[i]
            patient_data, patient_metadata = self.load_patient(j, k)

            if self.job_description['dataset'] == 'airway':
                if self._is_generate_errors_labels:
                    data_temp = patient_data[:-3]
                    seg_temp = patient_data[-3:]

                    labels = seg_temp[0]
                    ipatient = os.path.basename(j)

                    errors = self._random_error_generator(labels, ipatient)

                    # append the generated mask with errors in last channel of 'patient_seg'
                    seg_temp = np.concatenate((seg_temp, np.expand_dims(errors, axis=0)))

                    patient_data, patient_seg = crop(data_temp[None], seg_temp[None], self.patch_size, crop_type=self.crop)
                    data[i] = patient_data[0]
                    seg[i] = patient_seg[0]

                # ---------------------------------
                else:
                    patient_data, patient_seg = crop(patient_data[:-3][None], patient_data[-3:][None], self.patch_size,
                                                     crop_type=self.crop)

                    data[i] = patient_data[0]
                    seg[i] = patient_seg[0]

            metadata.append(patient_metadata)
            patient_names.append(j)

        return {'data': data, 'seg': seg, 'metadata': metadata, 'names': patient_names}


def get_train_transform(patch_size):
    tr_transforms = []
    tr_transforms.append(
        SpatialTransform_2(
            patch_size, [i // 2 for i in patch_size],
            do_elastic_deform=False, deformation_scale=(0, 0.25),
            do_rotation=True,
            angle_x=(- 30 / 360. * 2 * np.pi, 30 / 360. * 2 * np.pi),
            angle_y=(- 30 / 360. * 2 * np.pi, 30 / 360. * 2 * np.pi),
            angle_z=(- 30 / 360. * 2 * np.pi, 30 / 360. * 2 * np.pi),
            do_scale=True, scale=(0.7, 1.4),
            border_mode_data='constant', border_cval_data=0,
            border_mode_seg='constant', border_cval_seg=0,
            order_seg=1, order_data=3,
            random_crop=True,
            p_el_per_sample=0.0, p_rot_per_sample=0.2, p_scale_per_sample=0.2
        )
    )
    tr_transforms.append(MirrorTransform(axes=(0, 1, 2)))
    tr_transforms.append(GammaTransform(gamma_range=(0.7, 1.5), invert_image=False, per_channel=True, p_per_sample=0.3))
    tr_transforms = Compose(tr_transforms)
    return tr_transforms


def start_main_3D(job_description):
    # initialization
    model = LR_3D(job_description=job_description).to(job_description['device'])

    model.train()
    model_G = GAN_3D(job_description=job_description).to(job_description['device'])

    best_val = 0
    best_epoch = 0
    dict = defaultdict(list)

    mkdir.mkdir(job_description['result_path'] + '/model')
    mkdir.mkdir(job_description['result_path'] + '/preview_main/train')
    mkdir.mkdir(job_description['result_path'] + '/preview_main/val')

    # Dataloader setting
    patch_size = job_description['Patch_size']
    batch_size = job_description['batch_size']

    shapes = [AirwayDataLoader3D.load_patient_shape(i)[0].shape[1:] for i in job_description['patients_keys']]
    max_shape = np.max(shapes, 0)
    max_shape = np.max((max_shape, patch_size), 0)

    dataloader_train = AirwayDataLoader3D(job_description['train_keys'], job_description['train_initial_keys'],
                                          batch_size, max_shape, 1, job_description,
                                          seed_for_shuffle=job_description['seed'], modalities=1, mode='train',
                                          is_generates_errors_labels=True,
                                          path_basedir_info_gen_errors=job_description[
                                              'Patient_DIR']
                                          )
    dataloader_validation = AirwayDataLoader3D(job_description['val_keys'], job_description['val_initial_keys'],
                                               batch_size, patch_size, 1, job_description,
                                               seed_for_shuffle=job_description['seed'], shuffle=False, crop='center',
                                               modalities=1, mode='valid',
                                               is_generates_errors_labels=False,
                                               path_basedir_info_gen_errors=job_description[
                                                   'Patient_DIR']

                                               )
    tr_transforms = get_train_transform(patch_size)

    tr_gen = MultiThreadedAugmenter(dataloader_train, tr_transforms, num_processes=4,
                                    num_cached_per_queue=1,
                                    seeds=None, pin_memory=False)

    val_gen = MultiThreadedAugmenter(dataloader_validation, None,
                                     num_processes=4, num_cached_per_queue=1,
                                     seeds=None,
                                     pin_memory=False)
    tr_gen.restart()
    val_gen.restart()

    num_batches_per_epoch = job_description['number_training_batches_per_epoch']
    num_validation_batches_per_epoch = job_description['number_validation_batches_per_epoch']

    time_per_epoch = []
    start = time()

    criterion = (DiceCoefficientLF(),)
    eval_mode = eval_val.eval_val_LR_3D(job_description=job_description)
    tqiter = trange(job_description['epoch']-job_description['epoch_restart'], desc=job_description['dataset'])

    epoch_val = 0
    best_val = epoch_val

    for epoch in tqiter:
        epoch = epoch + job_description['epoch_restart']
        start_epoch = time()
        fig_loss = plt.figure(num='loss', figsize=[10, 3.8])
        epoch_train = 0

        model_G.load_state_dict(torch.load(job_description['Model_DIR'] + '/G_' + str(job_description['G_number']) + '.pth'))
        model_G.eval()

        if job_description['pretrain_main']:
            model.load_state_dict(torch.load(job_description['result_path'] + '/model/best_val.pth'))

        for b in range(num_batches_per_epoch):

            if job_description['dataset'] == 'airway':
                balance = random.uniform(0, 1)
            else:
                balance = 0.1

            if balance > 0.3:
                while True:
                    batch = next(tr_gen)
                    if job_description['dataset'] == 'airway':
                        inputs = torch.from_numpy(batch['data']).to(job_description['device'])
                        gt = torch.from_numpy(batch['seg']).to(job_description['device'], dtype=torch.long)[:, 0:1]
                        lungs = torch.from_numpy(batch['seg']).to(job_description['device'], dtype=torch.long)[:, 1:2]
                        initials = torch.from_numpy(batch['seg']).to(job_description['device'], dtype=torch.float)[:,
                                   2:3]
                        errors = torch.from_numpy(batch['seg']).to(job_description['device'], dtype=torch.float)[:, 3:4]
                        initials = initials * lungs

                    else:
                        inputs = torch.from_numpy(batch['data']).to(job_description['device'])
                        gt = torch.from_numpy(batch['seg']).to(job_description['device'], dtype=torch.long)[:, 0:1]
                        initials = torch.from_numpy(batch['seg']).to(job_description['device'], dtype=torch.float)[:,
                                   1:2]
                        errors = torch.from_numpy(batch['seg']).to(job_description['device'], dtype=torch.float)[:, 2:3]

                    if len(torch.unique(gt)) > 1:
                        break
            else:
                batch = next(tr_gen)
                if job_description['dataset'] == 'airway':
                    inputs = torch.from_numpy(batch['data']).to(job_description['device'])
                    gt = torch.from_numpy(batch['seg']).to(job_description['device'], dtype=torch.long)[:, 0:1]
                    lungs = torch.from_numpy(batch['seg']).to(job_description['device'], dtype=torch.long)[:, 1:2]
                    initials = torch.from_numpy(batch['seg']).to(job_description['device'], dtype=torch.float)[:, 2:3]
                    errors = torch.from_numpy(batch['seg']).to(job_description['device'], dtype=torch.float)[:, 3:4]
                    initials = initials * lungs

                else:
                    inputs = torch.from_numpy(batch['data']).to(job_description['device'])
                    gt = torch.from_numpy(batch['seg']).to(job_description['device'], dtype=torch.long)[:, 0:1]
                    initials = torch.from_numpy(batch['seg']).to(job_description['device'], dtype=torch.float)[:, 1:2]
                    errors = torch.from_numpy(batch['seg']).to(job_description['device'], dtype=torch.float)[:, 2:3]

            gt = one_hot.one_hot_embedding(gt, num_classes=2)
            gt = gt[:, 0].permute(0, 4, 1, 2, 3).to(job_description['device'], dtype=torch.float)

            optimizer = (optim.SGD(model.parameters(), lr=poly_lr(epoch, job_description['epoch'], job_description['lr'], 0.9), momentum=0.99, nesterov=True, weight_decay=3e-05), )
            optimizer[0].zero_grad()

            GD = 'G'
            outputs_rec, cla, gt_bool = model_G.forward(inputs, errors, initials, GD, epoch, job_description)

            select = random.randint(0, 10)

            outputs_rec = torch.where(outputs_rec >= 0.3, 1, 0)

            outputs_seg = model.forward(inputs, outputs_rec, initials, select)

            if job_description['dataset'] == 'airway':
                outputs_seg = outputs_seg * lungs

            loss = criterion[0](outputs_seg[:, :], gt[:, 1:2])
            loss.backward()
            optimizer[0].step()
            epoch_train += loss.item()

            # visualize training set at the end of each epoch
            if epoch % job_description['vis_train'] == job_description['vis_train'] - 1:
                if job_description['preview_train'] == True:
                    if b == 0:
                        slice = int(job_description['Patch_size'][0] / 2)
                        errors_vis = errors.cpu().detach().numpy()[0][0][slice]
                        inputs_vis = outputs_rec.cpu().detach().numpy()[0][0][slice]
                        initials_vis = initials.cpu().detach().numpy()[0][0][slice]

                        label_vis = gt.cpu().detach().numpy()[0][0][slice]
                        outputs_vis = outputs_seg.cpu().detach().numpy()[0][0][slice]

                        fig_batch = plt.figure(figsize=[7, 10])
                        visualize_LR(errors_vis, inputs_vis, initials_vis, label_vis, outputs_vis, epoch=epoch)

                        plt.savefig(job_description['result_path'] + '/preview_main/train/' + 'epoch_%s.jpg' % epoch)
                        plt.close(fig_batch)

        tqiter.set_description(job_description['dataset'] + '(train=%.4f, val=%.4f)'
                               % (epoch_train, epoch_val))

        if epoch % job_description['val_epoch'] == job_description['val_epoch'] - 1:
            epoch_val = eval_mode.eval_val(model, val_gen, num_validation_batches_per_epoch, epoch, job_description)

            if epoch_val > best_val:
                best_val = epoch_val
                best_epoch = epoch
                torch.save(model.state_dict(), job_description['result_path'] + '/model/best_val.pth')

        # save and visualize training information
        if epoch == 0:
            title = 'Epoch     Train     Val'    'best_epoch\n'
            history_log(job_description['result_path'] + '/history_log.txt', title, 'w')
            history = (
                '{:3d}        {:.4f}       {:.4f}       {:d}\n'
                    .format(epoch, epoch_train / (num_batches_per_epoch), epoch_val, best_epoch))
            history_log(job_description['result_path'] + '/history_log.txt', history, 'a')

            title = title.split()
            data = history.split()
            for ii, key in enumerate(title[:]):
                if ii == 0:
                    dict[key].append(int(data[ii]))
                else:
                    dict[key].append(float(data[ii]))
            visualize_loss(fig_loss, dict=dict, title=title, epoch=epoch)
            plt.savefig(job_description['result_path'] + '/Log.jpg')
            plt.close(fig_loss)

        elif epoch > 0:
            title = 'Epoch     Train     Val'    'best_epoch\n'
            history = (
                '{:3d}        {:.4f}       {:.4f}       {:d}\n'
                    .format(epoch, epoch_train / (num_batches_per_epoch), epoch_val, best_epoch))
            history_log(job_description['result_path'] + '/history_log.txt', history, 'a')

            title = title.split()
            data = history.split()
            for ii, key in enumerate(title[:]):
                if ii == 0:
                    dict[key].append(int(data[ii]))
                else:
                    dict[key].append(float(data[ii]))
            visualize_loss(fig_loss, dict=dict, title=title, epoch=epoch)
            plt.savefig(job_description['result_path'] + '/Log.jpg')
            plt.close(fig_loss)

        end_epoch = time()
        time_per_epoch.append(end_epoch - start_epoch)

    end = time()
    total_time = end - start
    print('-' * 64)
    print("Running %d epochs took a total of %.2f seconds with time per epoch being %s" %
          (job_description['epoch'], total_time, str(time_per_epoch)))
    print('main training finished')
    print('-' * 64)
