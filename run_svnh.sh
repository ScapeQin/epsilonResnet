# run epsilon ResNet 110 with epsilon=2.5
python cifar10-epsilon-resnet.py --gpu 2 -n 18 -e 2.5 -o cifar10-e_2.5-n_18
# compress the model of step 303420
python compressModel.py --dir train_log.cifar10-e_2.5-n_18 --step 303420
# inference on the compressed model of step 303420
python cifar10-compressed-resnet.py --cfg train_log.cifar10-e_2.5-n_18/cifar10-epsilon-resnet/compressed_model_303420.cfg --gpu 2 --cifar10 


# run epsilon ResNet 200 with epsilon=2.5
python cifar10-epsilon-resnet.py --gpu 2 -n 33 -e 2.5 -o cifar10-e_2.5-n_33
# run epsilon ResNet 500 with epsilon=2.5
python cifar10-epsilon-resnet.py --gpu 2 -n 83 -e 2.5 -o cifar10-e_2.5-n_83
# run epsilon ResNet 750 with epsilon=2.5
python cifar10-epsilon-resnet.py --gpu 2 -n 125 -e 2.5 -o cifar10-e_2.5-n_125

# Please change the parameter to compress other model and do inference
