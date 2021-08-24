
# You can check on Beluga:
# projects/rrg-descotea/datasets/ismrm2015/derivatives,
# and create your own data lists
# subjects: ismrm2015_noArtefact

# Run this from inside dwi_ml.

###########
# Organize data
###########
beluga="YOUR INFOS"
ismrm2015_folder="$beluga/ismrm2015/derivatives"
database_folder="$ismrm2015_folder/noArtefact"

subjects_list="$database_folder/subjects.txt"

rm -r $database_folder/dwi_ml_ready
organize_from_tractoflow_folder=please_copy_and_adapt/
organize_from_tractoflow_folder=../Learn2Track/scripts/
$organize_from_tractoflow_folder/organize_from_tractoflow.sh $database_folder $subjects_list

###########
# Create hdf5
###########
name=ismrm2015_noArtefact_test
bundle=tractoflow__interface_pft_tracking_wholebrain
logging="debug"
mask_for_standardization="masks/b0_bet_mask_resampled.nii.gz"
space="rasmm"
step_size=0.5 #Use step_size for now. Dipy's compress_streamlines seems to have memory issues.
training_subjs="$database_folder/subjects_for_ML_training.txt"
echo ismrm2015_noArtefact > $training_subjs
validation_subjs="$database_folder/subjects_for_ML_validation.txt"
echo fake_subj2 > $validation_subjs
cp -r $database_folder/dwi_ml_ready/ismrm2015_noArtefact/ $database_folder/dwi_ml_ready/fake_subj2
config_file="$database_folder/config_file.json"
# My config file is:
#{
#    "input": {
#	        "type": "volume",
#       	"files": ["dwi/dwi_tractoflow.nii.gz", "anat/t1_tractoflow.nii.gz", "anat/t1_tractoflow.nii.gz", "anat/t1_tractoflow.nii.gz"]
#	     }
#}

create_hdf5_dataset.py --force --name $name \
        --std_mask $mask_for_standardization \
        --bundles $bundle --logging $logging --space $space \
        --enforce_bundles_presence True --step_size $step_size \
        $database_folder $config_file $training_subjs $validation_subjs

###########
# Tests on dataset and batch sampler
###########
hdf5_filename="$database_folder/hdf5/$name.hdf5"
ref="$database_folder/dwi_ml_ready/ismrm2015_noArtefact/anat/t1_tractoflow.nii.gz"
test_tractograms_path="$database_folder/dwi_ml_ready/ismrm2015_noArtefact"
tests/test_multisubjectdataset_creation_from_hdf5.py $hdf5_filename
tests/test_batch_sampler_iter.py $hdf5_filename

# Open model.batch_samplers and change SAVE_BATCH_INPUT_MASK to True in both
# batch sampler and test file to save input masks. Run as is to see output
# shapes
tests/test_batch_sampler_load_batch.py $hdf5_filename $ref $test_tractograms_path

# check results and then:
# rm $database_folder/dwi_ml_ready/ismrm2015_noArtefact/test_batch1*

###########
# Running training on chosen database
###########
mkdir $database_folder/experiments
python please_copy_and_adapt/train_model.py --hdf5_filename $database_folder/hdf5/ismrm2015_noArtefact_test.hdf5 \
      --parameters_filename please_copy_and_adapt/training_parameters.yaml \
      --experiment_name test_experiment1 $database_folder/experiments
