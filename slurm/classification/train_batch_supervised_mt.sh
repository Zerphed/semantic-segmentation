#!/bin/bash

# Request 1 Tesla K80 or P100. The 'hsw' constraint guarantees K80 or P100 because they are the only GPU nodes with hsw processors
#SBATCH --gres=gpu:teslap100:1
#SBATCH -p gpu
#SBATCH --mem 42G
#SBATCH -c 8
#SBATCH -t 03-00:00:00
#SBATCH -D /scratch/work/jhnissin/semantic-segmentation/

module purge
module load anaconda2
module load teflon
source activate ss-new
module load CUDA/9.0.176 cuDNN/7-CUDA-9.0.176

srun python -m src.train --model enet-naive-upsampling-encoder-only --mfolder enet-naive-upsampling-encoder-only/supervised-mt --trainer classification_supervised_mean_teacher --config ./configs/config-classification.json --wdir /scratch/work/jhnissin/semantic-segmentation/ --maxjobs 6