import numpy as np
import partitioning
import pickle
import h5py
import pandas as pd
import gdal_tools

VGGM = -5.24
VGGS = 8.17
SATM = 8.00
SATS = 0.25

def map_labels(labels):
    return labels - 1

def soften_ordinal_labels(labels, m=0.05): # this function softens the ordinal labels for better training.
    labels_ = labels.copy()
    labels_[labels==1] = 1.0 - m
    for l in range(labels.shape[0]): # assuming first dimension is batch and second dimension is classes
        maxindex = np.argmax(labels[l])
        if maxindex == 0:
            labels_[l,1] = m
        elif maxindex == labels.shape[1]-1:
            labels_[l,-2] = m
        else:
            labels_[l,maxindex-1] = m / 2.0
            labels_[l,maxindex+1] = m / 2.0
    return labels_.astype(np.float32)

def normalize_features(x):
    return (x - VGGM) / VGGS

def normalize_satellite(x):
    return (x - SATM) / SATS

class Dataset:
    def __init__(self,
                image_hdf5_file,
                label_csv_file,
                label_name,
                sat_image,
                sat_patchsize,
                clabel_name=None):
        self.imf = h5py.File(image_hdf5_file, 'r')
        self.codes = self.imf['features']
        self.raw_label_data = pd.read_csv(label_csv_file)
        self.labels = map_labels(self.raw_label_data[label_name].to_numpy())
        self.vmap = self.raw_label_data[['img_id', 'pcd', 'oa11', 'lsoa11']].to_numpy()
        self.label_name=label_name
        # clabel_name is the name of the label that will be used to constrain partitioning.
        # e.g. Images coming from the same lsoa will not be separated during partitioning.
        # all of them will remain in the same partition.
        # if it is None, then no constrain is given.
        if clabel_name is not None:
            self.clabels = self.raw_label_data[clabel_name].to_numpy()
        else:
            self.clabels = None

        ## Satellite information
        # reading the lattitude and longitude information
        self.gsv_lat = self.raw_label_data['gsv_lat'].to_numpy()
        self.gsv_lng = self.raw_label_data['gsv_lng'].to_numpy()

        # the size of the satellite patch
        self.psat_size = sat_patchsize

        # reading the satellite images
        self.sat_raster_name = sat_image

        # population label distribution
        self.label_types, self.label_counts = np.unique(self.labels, return_counts=True)
        self.label_dist = self.label_counts / np.float(self.label_counts.sum())
        self.num_labels = self.label_types.size
        self.batch_inds = np.zeros(self.num_labels, dtype=np.int)
        print('Label types: {}'.format(self.label_types))
        print('Label counts: {}'.format(self.label_counts))
        print('Label distributions: {}'.format(self.label_dist))
        # = indices to keep track of which samples are used so far within
        # = training. important to count epochs, rather than iterations.
        self.batch_ind = 0
        self.batch_ind_test = 0
        self.batch_ind_valid = 0
        # = place holders
        self.train_part = []
        self.test_part = []
        self.validation_part = []

    # = this function gets features and labels of samples with ids in the
    # = list rows.
    def get_data_part(self, rows, noise_std=None):
        srows = sorted(rows)
        # getting data from the street view images
        d1 = normalize_features(self.codes[srows, 0, :])
        d2 = normalize_features(self.codes[srows, 1, :])
        d3 = normalize_features(self.codes[srows, 2, :])
        d4 = normalize_features(self.codes[srows, 3, :])
        if noise_std is not None:
            d1 += np.random.normal(loc=0, scale=0.05, size=d1.shape)
            d2 += np.random.normal(loc=0, scale=0.05, size=d2.shape)
            d3 += np.random.normal(loc=0, scale=0.05, size=d3.shape)
            d4 += np.random.normal(loc=0, scale=0.05, size=d4.shape)

        # getting the labels.
        l = np.zeros((d1.shape[0], self.num_labels), dtype=np.float)
        lpart = self.labels[srows]
        for lab_, k in zip(self.label_types, range(self.num_labels)):
            l[lpart == lab_, k] = 1

        # getting the satellite data
        lats = self.gsv_lat[srows]
        lngs = self.gsv_lng[srows]
        img = gdal_tools.get_patch_series(np.stack([lats, lngs], axis=1),
                                    self.sat_raster_name,
                                    self.psat_size, self.psat_size)
        img = normalize_satellite(np.log(img))
        return d1, d2, d3, d4, img, l

    def get_train_batch(self, batch_size):
        rows = self.train_part[self.batch_ind: self.batch_ind + batch_size]
        d1, d2, d3, d4, img, l = self.get_data_part(rows)
        self.batch_ind += batch_size
        if self.batch_ind >= len(self.train_part):
            self.batch_ind = 0
        return d1, d2, d3, d4, img, l

    def get_balanced_train_batch(self, batch_size):
        rows = []
        lsize = self.label_types.size
        lbatch_size = np.int(batch_size / np.float(lsize))
        for l in self.label_types:
            lrows = np.random.permutation(np.where(self.labels[self.train_part] == l)[0])[:lbatch_size].astype(np.int)
            rows += list(np.asarray(self.train_part)[lrows])
        d1, d2, d3, d4, img, l = self.get_data_part(rows)
        return d1, d2, d3, d4, img, l

    def get_train_data(self):
        rows = self.train_part
        d1, d2, d3, d4, img, l = self.get_data_part(rows)
        return d1, d2, d3, d4, img, l

    def get_validation_batch(self, batch_size):
        rows = self.validation_part[self.batch_ind_valid: self.batch_ind_valid + batch_size]
        d1, d2, d3, d4, img, l = self.get_data_part(rows)
        self.batch_ind_valid += batch_size
        if self.batch_ind_valid >= len(self.validation_part):
            self.batch_ind_valid = 0
        return d1, d2, d3, d4, img, l

    def get_balanced_validation_batch(self, batch_size):
        rows = []
        lsize = self.label_types.size
        lbatch_size = np.int(batch_size / np.float(lsize))
        for l in self.label_types:
            lrows = np.random.permutation(np.where(self.labels[self.validation_part] == l)[0])[:lbatch_size].astype(np.int)
            rows += list(np.asarray(self.validation_part)[lrows])
        d1, d2, d3, d4, img, l = self.get_data_part(rows)
        return d1, d2, d3, d4, img, l

    def get_validation_data(self):
        rows = self.validation_part
        d1, d2, d3, d4, img, l = self.get_data_part(rows)
        return d1, d2, d3, d4, img, l

    def get_test_batch(self, batch_size):
        rows = self.test_part[self.batch_ind_test: self.batch_ind_test + batch_size]
        d1, d2, d3, d4, img, l = self.get_data_part(rows)
        self.batch_ind_test += batch_size
        if self.batch_ind_test >= len(self.test_part):
            self.batch_ind_test = 0
        return d1, d2, d3, d4, img, l

    def get_test_data(self):
        rows = self.test_part
        d1, d2, d3, d4, img, l = self.get_data_part(rows)
        return d1, d2, d3, d4, img, l

    def test_iterator(self, batch_num=1):
        num_iter = np.int(np.ceil(len(self.test_part) / batch_num))
        for n in range(num_iter):
            rows = self.test_part[n*batch_num : (n+1)*batch_num]
            yield self.get_data_part(rows), self.vmap[sorted(rows),:]

    def validation_iterator(self, batch_num=1):
        num_iter = np.int(np.ceil(len(self.validation_part) / batch_num))
        for n in range(num_iter):
            rows = self.validation_part[n*batch_num : (n+1)*batch_num]
            yield self.get_data_part(rows)

    def write_preds(self, preds, fname):
        srows = sorted(self.test_part)
        data_matrix=np.append(self.vmap[srows,:],self.labels[srows,np.newaxis],axis=1)
        data_matrix=np.append(data_matrix,preds[:,np.newaxis],axis=1)
        pred_matrix=pd.DataFrame(data=data_matrix,columns=['img_id', 'pcd', 'oa11', 'lsoa11',self.label_name,'predicted'])
        pred_matrix.to_csv(fname,index=False)

class Dataset_CrossValidation(Dataset):
    def __init__(self, image_hdf5_file, label_csv_file, label_name, sat_image, sat_patchsize, clabel_name=None):
        Dataset.__init__(self,
                         image_hdf5_file,
                         label_csv_file,
                         label_name,
                         sat_image,
                         sat_patchsize,
                         clabel_name=clabel_name)

    def pick_label(self, part_gen, part_file, part_kn=5, part_kp=0, vsize=0.1, seed=None):
        '''
            This runs at every creation instance
            label_type: 'cat' (categorial), 'cont' (continuous)
        '''
        if part_gen == 1:
            # = this part creates partitions from the data and saves them in a specified file.
            # = the partitioning is k-folds and stratified.
            # = it also allows constraints, see above comment, as clabels.
            print('==================================== generating partitions from selected classes =====================================')
            self.kpartitions=partitioning.partition_stratified_kfold(part_kn,
                                                                     self.labels,
                                                                     seed=seed,
                                                                     clabels=self.clabels)

            print('==================================== generating partitions =====================================')
            self.kpartitions=partitioning.partition_stratified_kfold(part_kn,
                                                                     self.labels,
                                                                     seed=seed,
                                                                     clabels=self.clabels)
            pickle.dump(self.kpartitions, open(part_file, 'wb'))
        else:
            # = reads a partitioning that was written before.
            # = e.g. if 5 fold cross-validation is used, then this file simply
            # = will have 5 partitions written in it. self.kpartitions is a
            # = list with 5 members and each member has a list of data sample
            # = ids.
            self.kpartitions=pickle.load(open(part_file, 'rb'))

        # = creates training and test part from the self.kpartitions.
        # = e.g. in 5 fold cross validation, it uses the fold part_kp (1...5)
        # = as test and combines the remaining 4 as training data.
        _train_part, self.test_part = partitioning.get_partition_stratified_kfold(part_kp, self.kpartitions)

        
        # = vsize indicates the portion of the training set that will be used as validation. 
        # = the default value is set to 0.1, meaning 10% of all training examples.
        if vsize > 0.0:
            self.train_part, self.validation_part = partitioning.decimate_partition_stratified(_train_part, self.labels, psize=1.0-vsize, clabels=self.clabels)
        else:
            self.train_part = _train_part

# = this class simply divides the dataset into three classes:
# == Train (T)
# == Validation (V)
# == Test (T)
class Dataset_TVT(Dataset):
    def __init__(self, image_hdf5_file, label_csv_file, label_name, sat_image, sat_patchsize, clabel_name=None):
        Dataset.__init__(self,
                         image_hdf5_file,
                         label_csv_file,
                         label_name,
                         sat_image,
                         sat_patchsize,
                         clabel_name=clabel_name)

    def pick_label(self, part_gen, part_file, train_size, valid_size, psize=1.0, seed=None):
        '''
            This runs at every creation instance
            label_type: 'cat' (categorial), 'cont' (continuous)
        '''
        if part_gen == 1:
            # = this part creates partitions from the data and saves them in a specified file.
            # = the partitioning is stratified and only 3 parts: train, test and validation.
            # = it also allows constrains, see above comment, as clabels.
            print('==================================== generating partitions =====================================')
            _train_part, self.validation_part, self.test_part = partitioning.partition_stratified_validation(self.labels,
                                                                                                                 train_size,
                                                                                                                 valid_size,
                                                                                                                 seed=seed,
                                                                                                                 clabels=self.clabels)
            pickle.dump(_train_part, open(part_file + '_train', 'wb'))
            pickle.dump(self.validation_part, open(part_file + '_validation', 'wb'))
            pickle.dump(self.test_part, open(part_file + '_test', 'wb'))
        else:
            # = reads a partitioning that was written before.
            # = there are three files: validation, test and train
            # = this part reads all of them.
            _train_part=pickle.load(open(part_file + '_train', 'rb'))
            self.validation_part=pickle.load(open(part_file + '_validation', 'rb'))
            self.test_part=pickle.load(open(part_file + '_test', 'rb'))


        # = psize indicates the percentage of training data to be used during
        # = training. If it is 1.0, then we use all the training data. So,
        # = self.train_part = _train_part
        # = if it is less then 1.0 then we take a subset of the training data
        # = with the same proportions of classes, i.e. stratified.
        # = Note that inside decimate_partition_stratified code, we randomly
        # = permute over the samples. So, every time you run this code,
        # = training will happen with another subset of size psize.
        if psize < 1.0:
            self.train_part = partitioning.decimate_partition_stratified(_train_part, self.labels, psize=psize)
        else:
            self.train_part = _train_part

    def write_preds_validation(self, preds, fname):
        srows = sorted(self.validation_part)
        data_matrix=np.append(self.vmap[srows,:],self.labels[srows,np.newaxis],axis=1)
        data_matrix=np.append(data_matrix,preds[:,np.newaxis],axis=1)
        pred_matrix=pd.DataFrame(data=data_matrix,columns=['img_id', 'pcd', 'oa11', 'lsoa11',self.label_name,'predicted'])
        pred_matrix.to_csv(fname,index=False)

# = this class simply divides the dataset into two classes:
# == Train (T)
# == Test (T)
class Dataset_TT(Dataset):
    def __init__(self, image_hdf5_file, label_csv_file, label_name, sat_image, sat_patchsize, clabel_name=None):
        Dataset.__init__(self,
                         image_hdf5_file,
                         label_csv_file,
                         label_name,
                         sat_image,
                         sat_patchsize,
                         clabel_name=clabel_name)

    def pick_label(self, part_gen, part_file, train_size, psize=1.0, seed=None):
        '''
            This runs at every creation instance
            label_type: 'cat' (categorial), 'cont' (continuous)
        '''

        if part_gen == 1:
            # = this part creates partitions from the data and saves them in a specified file.
            # = the partitioning is stratified and only 2 parts: train, and test .

            print('==================================== generating partitions =====================================')
            _train_part, self.test_part = partitioning.partition_stratified(self.labels,
                                                                            train_size,
                                                                            seed=seed,
                                                                            clabels=self.clabels)
            pickle.dump(_train_part, open(part_file + '_train', 'wb'))
            pickle.dump(self.test_part, open(part_file + '_test', 'wb'))
        else:
            # = reads a partitioning that was written before.
            # = there are three files: validation, test and train
            # = this part reads all of them.
            _train_part=pickle.load(open(part_file + '_train', 'rb'))
            self.test_part=pickle.load(open(part_file + '_test', 'rb'))


        # = psize indicates the percentage of training data to be used during
        # = training. If it is 1.0, then we use all the training data. So,
        # = self.train_part = _train_part
        # = if it is less then 1.0 then we take a subset of the training data
        # = with the same proportions of classes, i.e. stratified.
        # = Note that inside decimate_partition_stratified code, we randomly
        # = permute over the samples. So, every time you run this code,
        # = training will happen with another subset of size psize.
        if psize < 1.0:
            self.train_part = partitioning.decimate_partition_stratified(_train_part, self.labels, psize=psize)
        else:
            self.train_part = _train_part

    def get_validation_batch(self, batch_size):
        return self.get_train_batch(batch_size)

    def get_balanced_validation_batch(self, batch_size):
        return self.get_balanced_train_batch(batch_size)

    def write_preds_validation(self, preds, fname):
        srows = sorted(self.validation_part)
        data_matrix=np.append(self.vmap[srows,:],self.labels[srows,np.newaxis],axis=1)
        data_matrix=np.append(data_matrix,preds[:,np.newaxis],axis=1)
        pred_matrix=pd.DataFrame(data=data_matrix,columns=['img_id', 'pcd', 'oa11', 'lsoa11',self.label_name,'predicted'])
        pred_matrix.to_csv(fname,index=False)


# = this class simply divides the dataset into two classes using a class label:
# == Train (T)
# == Test (T) : test set consists of only the selected class label

class Dataset_TT_byclass(Dataset):
    def __init__(self, image_hdf5_file, label_csv_file, label_name, sat_image, sat_patchsize, clabel_name=None, label_test=None):
        Dataset.__init__(self,
                         image_hdf5_file,
                         label_csv_file,
                         label_name,
                         sat_image,
                         sat_patchsize,
                         clabel_name=clabel_name)
        self.label_test = label_test

    def pick_label(self, part_gen, part_file, train_size, psize=1.0, seed=None):
        '''
            This runs at every creation instance
            label_type: 'cat' (categorial), 'cont' (continuous)
        '''


        if part_gen == 1:
            # = this part creates partitions from the data and saves them in a specified file.
            # = the partitioning is stratified and only 2 parts: train, and test .

            print('==================================== generating partitions =====================================')
            _train_part, self.test_part = partitioning.partition_by_class(self.labels,
                                                                            self.label_test,
                                                                            seed=seed)
            pickle.dump(_train_part, open(part_file + '_train', 'wb'))
            pickle.dump(self.test_part, open(part_file + '_test', 'wb'))

        else:
            # = reads a partitioning that was written before.
            # = there are three files: validation, test and train
            # = this part reads all of them.
            _train_part=pickle.load(open(part_file + '_train', 'rb'))
            self.test_part=pickle.load(open(part_file + '_test', 'rb'))


        # = psize indicates the percentage of training data to be used during
        # = training. If it is 1.0, then we use all the training data. So,
        # = self.train_part = _train_part
        # = if it is less then 1.0 then we take a subset of the training data
        # = with the same proportions of classes, i.e. stratified.
        # = Note that inside decimate_partition_stratified code, we randomly
        # = permute over the samples. So, every time you run this code,
        # = training will happen with another subset of size psize.
        if psize < 1.0:
            self.train_part = partitioning.decimate_partition_stratified(_train_part, self.labels, psize=psize)
        else:
            self.train_part = _train_part

    def get_validation_batch(self, batch_size):
        return self.get_train_batch(batch_size)

    def get_balanced_validation_batch(self, batch_size):
        return self.get_balanced_train_batch(batch_size)

    def write_preds_validation(self, preds, fname):
        srows = sorted(self.validation_part)
        data_matrix=np.append(self.vmap[srows,:],self.labels[srows,np.newaxis],axis=1)
        data_matrix=np.append(data_matrix,preds[:,np.newaxis],axis=1)
        pred_matrix=pd.DataFrame(data=data_matrix,columns=['img_id', 'pcd', 'oa11', 'lsoa11',self.label_name,'predicted'])
        pred_matrix.to_csv(fname,index=False)
